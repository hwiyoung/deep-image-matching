from pathlib import Path
from pprint import pprint
import os
import cv2
import h5py
import numpy as np
from tqdm import tqdm
from pathlib import Path
import shutil

from . import extractors, logger, matchers
from .extractors.extractor_base import extractor_loader
from .io.h5 import get_features, get_matches
from .matchers.matcher_base import matcher_loader
from .utils.consts import GeometricVerification, Quality, TileSelection
from .utils.geometric_verification import geometric_verification
from .utils.image import ImageList
from .utils.pairs_generator import PairsGenerator

from .extractors.superpoint import SuperPointExtractor
from .matchers.lightglue import LightGlueMatcher


def make_correspondence_matrix(matches: np.ndarray) -> np.ndarray:
    kpts_number = matches.shape[0]
    n_tie_points = np.arange(kpts_number).reshape((-1, 1))
    matrix = np.hstack((n_tie_points, matches.reshape((-1, 1))))
    correspondences = matrix[~np.any(matrix == -1, axis=1)]
    return correspondences


def apply_geometric_verification(
    kpts0: np.ndarray, kpts1: np.ndarray, correspondences: np.ndarray, config: dict
) -> np.ndarray:
    mkpts0 = kpts0[correspondences[:, 0]]
    mkpts1 = kpts1[correspondences[:, 1]]
    _, inlMask = geometric_verification(
        kpts0=mkpts0,
        kpts1=mkpts1,
        method=config["geom_verification"],
        threshold=config["gv_threshold"],
        confidence=config["gv_confidence"],
    )
    return correspondences[inlMask]


def get_pairs_from_file(pair_file: Path) -> list:
    pairs = []
    with open(pair_file, "r") as txt_file:
        lines = txt_file.readlines()
        for line in lines:
            im1, im2 = line.strip().split(" ", 1)
            pairs.append((im1, im2))
    return pairs


class ImageMatching:
    default_conf_general = {
        "quality": Quality.MEDIUM,
        "tile_selection": TileSelection.NONE,
        "geom_verification": GeometricVerification.PYDEGENSAC,
        "output_dir": "output",
        "tiling_grid": [1, 1],
        "tiling_overlap": 0,
        "force_cpu": False,
        "do_viz": False,
        "fast_viz": True,
        "hide_matching_track": True,
        "do_viz_tiles": False,
    }

    def __init__(
        # TODO: add default values for not necessary parameters
        self,
        imgs_dir: Path,
        output_dir: Path,
        matching_strategy: str,
        retrieval_option: str,
        local_features: str,
        matching_method: str,
        pair_file: Path = None,
        custom_config: dict = {},
        overlap: int = 1,
    ):
        self.image_dir = Path(imgs_dir)
        self.output_dir = Path(output_dir)
        self.matching_strategy = matching_strategy
        self.retrieval_option = retrieval_option
        self.local_features = local_features
        self.matching_method = matching_method
        self.pair_file = Path(pair_file) if pair_file is not None else None
        self.overlap = overlap

        # Merge default and custom config
        self.custom_config = custom_config
        self.custom_config["general"] = {
            **self.default_conf_general,
            **custom_config["general"],
        }

        # Check that parameters are valid
        if retrieval_option == "sequential":
            if overlap is None:
                raise ValueError(
                    "'overlap' option is required when 'strategy' is set to sequential"
                )
        elif retrieval_option == "custom_pairs":
            if self.pair_file is None:
                raise ValueError(
                    "'pair_file' option is required when 'strategy' is set to custom_pairs"
                )
            else:
                if not self.pair_file.exists():
                    raise ValueError(f"File {self.pair_file} does not exist")

        # Initialize ImageList class
        self.image_list = ImageList(imgs_dir)
        images = self.image_list.img_names
        if len(images) == 0:
            raise ValueError(
                "Image folder empty. Supported formats: '.jpg', '.JPG', '.png'"
            )
        elif len(images) == 1:
            raise ValueError("Image folder must contain at least two images")

        # Initialize output directory
        self.output_dir.mkdir(parents=True, exist_ok=True)

        # Initialize extractor
        try:
            Extractor = extractor_loader(extractors, self.local_features)
        except AttributeError:
            raise ValueError(
                f"Invalid local feature extractor. {self.local_features} is not supported."
            )
        self._extractor = Extractor(self.custom_config)

        # Initialize matcher
        try:
            Matcher = matcher_loader(matchers, self.matching_method)
        except AttributeError:
            raise ValueError(
                f"Invalid matcher. {self.local_features} is not supported."
            )
        if self.matching_method == "lightglue":
            self._matcher = Matcher(
                local_features=self.local_features, config=self.custom_config
            )
        else:
            self._matcher = Matcher(self.custom_config)

        # Print configuration
        logger.info("Running image matching with the following configuration:")
        logger.info(f"  Image folder: {self.image_dir}")
        logger.info(f"  Output folder: {self.output_dir}")
        logger.info(f"  Number of images: {len(self.image_list)}")
        logger.info(f"  Matching strategy: {self.matching_strategy}")
        logger.info(f"  Retrieval option: {self.retrieval_option}")
        logger.info(f"  Overlap: {self.overlap}")
        logger.info(f"  Feature extraction method: {self.local_features}")
        logger.info(f"  Matching method: {self.matching_method}")

    @property
    def img_names(self):
        return self.image_list.img_names

    def generate_pairs(self):
        if self.pair_file is not None and self.matching_strategy == "custom_pairs":
            if not self.pair_file.exists():
                raise FileExistsError(f"File {self.pair_file} does not exist")

            pairs = get_pairs_from_file(self.pair_file)
            self.pairs = [
                (self.image_dir / im1, self.image_dir / im2) for im1, im2 in pairs
            ]

        else:
            pairs_generator = PairsGenerator(
                self.image_list.img_paths,
                self.matching_strategy,
                self.retrieval_option,
                self.overlap,
            )
            self.pairs = pairs_generator.run()
            with open(self.pair_file, "w") as txt_file:
                for pair in self.pairs:
                    txt_file.write(f"{pair[0].name} {pair[1].name}\n")

        return self.pair_file
    
    def rotate_upright_images(self):
        path_to_upright_dir = self.output_dir / "upright_images"        
        os.makedirs(path_to_upright_dir, exist_ok=False)
        images = os.listdir(self.image_dir)
        processed_images = []

        for img in images:
            shutil.copy(self.image_dir / img, path_to_upright_dir / img)
        
        rotations = [0, 90, 180, 270]
        cv2_rot_params = [None, cv2.ROTATE_90_CLOCKWISE, cv2.ROTATE_180, cv2.ROTATE_90_COUNTERCLOCKWISE]
        self.rotated_images = []
        SPextractor = SuperPointExtractor({})
        LGmatcher = LightGlueMatcher()
        features = {
            "feat0" : None,
            "feat1" : None,
        }
        for pair in self.pairs:
            matchesXrotation = []

            # Reference image
            ref_image = pair[0].name
            image0 = cv2.imread(str(path_to_upright_dir / ref_image))
            H, W = image0.shape[:2]
            new_width = 500
            new_height = int(H * 500/W)
            image0 = cv2.resize(image0, (new_width, new_height))
            image0 = cv2.cvtColor(image0, cv2.COLOR_BGR2GRAY)
            features["feat0"] = SPextractor._extract(image0)

            # Target image - find the best rotation
            target_img = pair[1].name
            if target_img not in processed_images:
                #processed_images.append(target_img)
                image1 = cv2.imread(str(path_to_upright_dir / target_img))
                for rotation, cv2rotation in zip(rotations, cv2_rot_params):
                    H, W = image1.shape[:2]
                    new_width = 500
                    new_height = int(H * 500/W)
                    _image1 = cv2.resize(image1, (new_width, new_height))
                    _image1 = cv2.cvtColor(_image1, cv2.COLOR_BGR2GRAY)
                    #rotation_matrix = cv2.getRotationMatrix2D((new_width / 2, new_height / 2), rotation, 1.0)
                    #rotated_image = cv2.warpAffine(image, rotation_matrix, (new_width, new_height))
                    if rotation != 0:
                        _image1 = cv2.rotate(_image1, cv2rotation)
                    features["feat1"] = SPextractor._extract(_image1)
                    matches = LGmatcher._match_pairs(features["feat0"],features["feat1"])
                    matchesXrotation.append((rotation, matches.shape[0]))
                index_of_max = max(range(len(matchesXrotation)), key=lambda i: matchesXrotation[i][1])
                n_matches = matchesXrotation[index_of_max][1]
                if index_of_max != 0 and n_matches>100:
                    processed_images.append(target_img)
                    self.rotated_images.append((pair[1].name, rotations[index_of_max]))
                    rotated_image1 = cv2.rotate(image1, cv2_rot_params[index_of_max])
                    cv2.imwrite(str(path_to_upright_dir / target_img), rotated_image1)

        out_file = self.pair_file.parent / f"{self.pair_file.stem}_rot.txt"
        with open(out_file, "w") as txt_file:
            for element in self.rotated_images:
                txt_file.write(f"{element[0]} {element[1]}\n")
        
        # Update image directory to the dir with upright images
        # Features will be rotate accordingly on exporting, if the images have been rotated
        self.image_dir = path_to_upright_dir
        self.image_list = ImageList(path_to_upright_dir)
        images = self.image_list.img_names


    def extract_features(self) -> Path:
        logger.info(f"Extracting features with {self.local_features}...")
        logger.info(f"{self.local_features} configuration: ")
        pprint(self.custom_config["extractor"])

        # Extract features
        for img in tqdm(self.image_list):
            feature_path = self._extractor.extract(img)

        logger.info("Features extracted!")

        return feature_path

    def match_pairs(self, feature_path: Path) -> Path:
        logger.info(f"Matching features with {self.matching_method}...")
        logger.info(f"{self.matching_method} configuration: ")
        pprint(self.custom_config["matcher"])
        # Check that feature_path exists
        feature_path = Path(feature_path)
        if not feature_path.exists():
            raise ValueError(f"Feature path {feature_path} does not exist")

        # Define matches path
        matches_path = feature_path.parent / "matches.h5"

        # Match pairs
        logger.info("Matching features...")
        logger.info("")
        for pair in tqdm(self.pairs):
            logger.debug(f"Matching image pair: {pair[0].name} - {pair[1].name}")
            im0 = self.image_dir / pair[0].name
            im1 = self.image_dir / pair[1].name

            # Run matching
            matches = self._matcher.match(
                feature_path=feature_path,
                matches_path=matches_path,
                img0=im0,
                img1=im1,
            )

            if matches is None:
                continue

            # Get original keypoints from h5 file
            kpts0 = get_features(feature_path, im0.name)["keypoints"]
            kpts1 = get_features(feature_path, im1.name)["keypoints"]
            correspondences = get_matches(matches_path, im0.name, im1.name)

            # Apply geometric verification
            correspondences_cleaned = apply_geometric_verification(
                kpts0=kpts0,
                kpts1=kpts1,
                correspondences=correspondences,
                config=self.custom_config["general"],
            )

            # Update matches in h5 file
            with h5py.File(str(matches_path), "a", libver="latest") as fd:
                group = fd.require_group(im0.name)
                if im1.name in group:
                    del group[im1.name]
                group.create_dataset(im1.name, data=correspondences_cleaned)

            logger.debug(f"Pairs: {pair[0].name} - {pair[1].name} done.")

        logger.info("Matching done!")

        return matches_path

    def rotate_back_features(self, feature_path : Path) -> None:
        #images = self.image_list.img_names
        for img, theta in self.rotated_images:
            features = get_features(feature_path, img)
            keypoints = features["keypoints"]
            rotated_keypoints = np.empty(keypoints.shape)
            im = cv2.imread(str(self.image_dir / img))
            H, W = im.shape[:2]

            if theta == 180:
                for r in range(keypoints.shape[0]):
                    x, y = keypoints[r, 0], keypoints[r, 1]
                    y_rot = H - y
                    x_rot = W - x
                    rotated_keypoints[r, 0], rotated_keypoints[r, 1] = x_rot, y_rot

            if theta == 90:
                for r in range(keypoints.shape[0]):
                    x, y = keypoints[r, 0], keypoints[r, 1]
                    y_rot = W - x
                    x_rot = y
                    rotated_keypoints[r, 0], rotated_keypoints[r, 1] = x_rot, y_rot

            if theta == 270:
                for r in range(keypoints.shape[0]):
                    x, y = keypoints[r, 0], keypoints[r, 1]
                    y_rot = x
                    x_rot = H - y
                    rotated_keypoints[r, 0], rotated_keypoints[r, 1] = x_rot, y_rot
            
            with h5py.File(feature_path, "r+", libver="latest") as fd:
                del fd[img]
                features["keypoints"] = rotated_keypoints
                grp = fd.create_group(img)
                for k, v in features.items():
                    if k == 'im_path' or k == 'feature_path':
                        grp.create_dataset(k, data=str(v))
                    if isinstance(v, np.ndarray):
                        grp.create_dataset(k, data=v)
