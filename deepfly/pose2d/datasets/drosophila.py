from __future__ import print_function, absolute_import

import torch.utils.data as data

from deepfly.GUI.util.os_util import *
from deepfly.pose2d.utils.osutils import isfile
from deepfly.pose2d.utils.transforms import *
from deepfly.GUI.Config import config

import glob

FOLDER_NAME = 0
IMAGE_NAME = 1


class Drosophila(data.Dataset):
    def __init__(
        self,
        data_folder,
        data_corr_folder=None,
        img_res=None,
        hm_res=None,
        train=True,
        sigma=1,
        jsonfile="drosophilaimaging-export.json",
        session_id_train_list=None,
        folder_train_list=None,
        augmentation=False,
        evaluation=False,
        unlabeled=None,
        num_classes=config["num_predict"],
        max_img_id=None,
    ):
        self.train = train
        self.data_folder = data_folder  # root image folders
        self.data_corr_folder = data_corr_folder
        self.json_file = os.path.join(jsonfile)
        self.is_train = train  # training set or test set
        self.img_res = img_res
        self.hm_res = hm_res
        self.sigma = sigma
        self.augmentation = augmentation
        self.evaluation = evaluation
        self.unlabeled = unlabeled
        self.num_classes = num_classes
        self.max_img_id = max_img_id
        self.cidread2cid = dict()

        self.session_id_train_list = session_id_train_list
        self.folder_train_list = folder_train_list
        assert (
            not self.evaluation or not self.augmentation
        )  # self eval then not augmentation
        assert not self.unlabeled or evaluation  # if unlabeled then evaluation

        manual_path_list = ["/data/paper/"]

        self.annotation_dict = dict()
        self.multi_view_annotation_dict = dict()

        # parse json file annotations
        if not self.unlabeled and isfile(self.json_file):
            json_data = json.load(open(self.json_file, "r"))
            for session_id in json_data.keys():
                for folder_name in json_data[session_id]["data"].keys():
                    if folder_name not in self.folder_train_list:
                        continue
                    for image_name in json_data[session_id]["data"][folder_name].keys():
                        key = ("/data/annot/" + folder_name, image_name)
                        # for the hand annotations, it is always correct ordering
                        self.cidread2cid[key[FOLDER_NAME]] = np.arange(
                            config["num_cameras"]
                        )
                        cid_read, img_id = parse_img_name(image_name)
                        if cid_read == 3:
                            continue
                        pts = json_data[session_id]["data"][folder_name][
                                image_name
                            ]["position"]
                        self.annotation_dict[key] = np.array(pts)

        # read the manual correction for training data
        print("Searching for manual corrections")
        n_joints = set()
        if not self.unlabeled and self.train:
            pose_corr_path_list = []
            for root in manual_path_list:
                print(
                    "Searching recursively: {}".format(
                        root
                    )
                )
                pose_corr_path_list.extend(
                    list(
                        glob.glob(
                            os.path.join(root, "./**/pose_corr*.pkl"), recursive=True
                        )
                    )
                )
                print(
                    "Number of manual correction files: {}".format(
                        len(pose_corr_path_list)
                    )
                )
            for path in pose_corr_path_list:
                d = pickle.load(open(path, "rb"))
                folder_name = d["folder"]
                key_folder_name = folder_name
                if folder_name not in self.cidread2cid:
                    cidread2cid, cid2cidread = read_camera_order(folder_name)
                    self.cidread2cid[key_folder_name] = cidread2cid
                for cid in range(config["num_cameras"]):
                    for img_id, points2d in d[cid].items():
                        cid_read = self.cidread2cid[key_folder_name].tolist().index(cid)
                        key = (key_folder_name, constr_img_name(cid_read, img_id))
                        num_heatmaps = points2d.shape[0]
                        n_joints.add(num_heatmaps)

                        pts = np.zeros((2 * self.num_classes, 2), dtype=np.float)
                        if cid < 3:
                            pts[: num_heatmaps // 2, :] = points2d[
                                : num_heatmaps // 2, :
                            ]
                        elif 3 < cid < 7:
                            continue
                            pts[
                                num_classes : num_classes + (num_heatmaps // 2), :
                            ] = points2d[num_heatmaps // 2 :, :]
                        elif cid==3:
                            continue
                        else:
                            raise NotImplementedError

                        self.annotation_dict[key] = pts

        if self.unlabeled:
            image_folder_path = os.path.join(self.unlabeled)
            cidread2cid, cid2cidread = read_camera_order(image_folder_path)
            self.cidread2cid[self.unlabeled] = cidread2cid

            for image_name_jpg in os.listdir(image_folder_path):
                if image_name_jpg.endswith(".jpg"):
                    image_name = image_name_jpg.replace(".jpg", "")
                    key = (self.unlabeled, image_name)
                    cid_read, img_id = parse_img_name(image_name)
                    if cidread2cid.tolist().index(cid_read) == 3:
                        continue
                    if self.max_img_id is not None and img_id > self.max_img_id:
                        continue
                    self.annotation_dict[key] = np.zeros(shape=(config["skeleton"].num_joints, 2))

        # make sure data is in the folder
        for folder_name, image_name in self.annotation_dict.copy().keys():
            cid_read, img_id = parse_img_name(image_name)

            image_file_pad = os.path.join(
                self.data_folder,
                folder_name.replace("_network", ""),
                constr_img_name(cid_read, img_id) + ".jpg",
            )
            image_file = os.path.join(
                self.data_folder,
                folder_name.replace("_network", ""),
                constr_img_name(cid_read, img_id, pad=False) + ".jpg",
            )

            if not (os.path.isfile(image_file) or os.path.isfile(image_file_pad)):
                self.annotation_dict.pop((folder_name, image_name), None)
                print("FileNotFound: {}/{} ".format(folder_name, image_name))
        """
        There are three cases:
        30: 15x2 5 points in each 3 legs, on 2 sides
        32: 15 tracked points,, plus antenna on each side
        38: 15 tracked points, then 3 stripes, then one antenna
        """
        # preprocess the annotations
        for k, v in self.annotation_dict.copy().items():
            cid_read, img_id = parse_img_name(k[IMAGE_NAME])
            folder_name = k[FOLDER_NAME]
            cid = self.cidread2cid[folder_name][cid_read] if cid_read != 7 else 3
            if "annot" in k[FOLDER_NAME]:
                if cid > 3:  # then right camera
                    v[:15, :] = v[15:30:]
                    v[15] = v[35]  # 35 is the second antenna
                elif cid < 3:
                    v[15] = v[30]  # 30 is the first antenna
                elif cid== 3 or cid == 7:
                    pass
                else:
                    raise NotImplementedError
                v[16:, :] = 0.0
            else:  # then manual correction
                if cid > 3:  # then right camera
                    v[: self.num_classes, :] = v[self.num_classes :, :]
                else:
                    v[: self.num_classes, :] = v[: self.num_classes, :]
                if cid == 3:
                    continue
                # contains only 3 legs in any of the sides
                v = v[: self.num_classes, :]  # keep only one side

            j_keep = np.arange(
                0, self.num_classes
            )  # removing first two joints from each leg
            v = v[j_keep, :]
            v = np.abs(v)  # removing low-confidence
            # make sure normalized
            assert np.logical_or(0 <= v, v <= 1).all()

            self.annotation_dict[k] = v

        self.annotation_key = list(self.annotation_dict.keys())
        if self.evaluation:  # sort keys
            self.annotation_key.sort(
                key=lambda x: x[0] + "_" + x[1].split("_")[3] + "_" + x[1].split("_")[1]
            )

        self.mean, self.std = self._compute_mean()

        print(
            "Folders inside {} data: {}".format(
                "train" if self.train else "validation",
                set([k[0] for k in self.annotation_key]),
            )
        )
        print("Successfully imported {} Images in Drosophila Dataset".format(len(self)))

    def _compute_mean(self):
        file_path = os.path.abspath(os.path.dirname(__file__))
        meanstd_file = config["mean"]
        if isfile(meanstd_file):
            meanstd = torch.load(meanstd_file)
        else:
            raise FileNotFoundError
            mean = torch.zeros(3)
            std = torch.zeros(3)
            for k in self.annotation_key:
                img_path = os.path.join(
                    self.data_folder, k[FOLDER_NAME], k[IMAGE_NAME] + ".jpg"
                )
                img = load_image(img_path)  # CxHxW
                mean += img.view(img.size(0), -1).mean(1)
                std += img.view(img.size(0), -1).std(1)
            mean /= len(self)
            std /= len(self)
            meanstd = {"mean": mean, "std": std}
            torch.save(meanstd, meanstd_file)
        if self.is_train:
            print(
                "    Mean: %.4f, %.4f, %.4f"
                % (meanstd["mean"][0], meanstd["mean"][1], meanstd["mean"][2])
            )
            print(
                "    Std:  %.4f, %.4f, %.4f"
                % (meanstd["std"][0], meanstd["std"][1], meanstd["std"][2])
            )

        return meanstd["mean"], meanstd["std"]

    def __get_image_path(self, folder_name, camera_id, pose_id, pad=True):
        img_path = os.path.join(
            self.data_folder,
            folder_name.replace("_network", ""),
            constr_img_name(camera_id, pose_id, pad=pad) + ".jpg",
        )
        return img_path

    def __getitem__(self, index, batch_mode=True, temporal=False):
        folder_name, img_name = (
            self.annotation_key[index][FOLDER_NAME],
            self.annotation_key[index][IMAGE_NAME],
        )
        cid_read, pose_id = parse_img_name(img_name)
        cid = self.cidread2cid[folder_name][cid_read]
        flip = cid in config["flip_cameras"] and ("annot" in folder_name or ( "annot" not in folder_name and self.unlabeled))

        try:
            img_orig = load_image(self.__get_image_path(folder_name, cid_read, pose_id))
        except FileNotFoundError:
            try:
                img_orig = load_image(
                    self.__get_image_path(folder_name, cid_read, pose_id, pad=False)
                )
            except FileNotFoundError:
                print(
                    "Cannot read index {} {} {} {}".format(
                        index, folder_name, cid_read, pose_id
                    )
                )
                return self.__getitem__(index + 1)

        pts = torch.Tensor(self.annotation_dict[self.annotation_key[index]])
        nparts = pts.size(0)
        assert (nparts == config["num_predict"])

        joint_exists = np.zeros(shape=(nparts,), dtype=np.uint8)
        for i in range(nparts):
            # we convert to int as we cannot pass boolean from pytorch dataloader
            # as we decrease the number of joints to skeleton.num_joints during training
            joint_exists[i] = (
                1
                if (
                    (0.01 < pts[i][0] < 0.99)
                    and (0.01 < pts[i][1] < 0.99)
                    and (
                            config["skeleton"].camera_see_joint(cid, i)
                            or config["skeleton"].camera_see_joint(
                            cid, (i + config["num_predict"])
                        )
                    )
                )
                else 0
            )
        if flip:
            img_orig = torch.from_numpy(fliplr(img_orig.numpy())).float()
            pts = shufflelr(pts, width=img_orig.size(2), dataset="drosophila")
        img_norm = im_to_torch(scipy.misc.imresize(img_orig, self.img_res))

        # Generate ground truth heatmap
        tpts = pts.clone()
        target = torch.zeros(nparts, self.hm_res[0], self.hm_res[1])

        for i in range(nparts):
            if joint_exists[i] == 1:
                tpts = to_torch(
                    pts * to_torch(np.array([self.hm_res[1], self.hm_res[0]])).float()
                )
                target[i] = draw_labelmap(
                    target[i], tpts[i], self.sigma, type='Gaussian'
                )
            else:
                # to make invisible joints explicit in the training visualization
                # these values are not used to calculate loss
                target[i] = torch.ones_like(target[i])

        # augmentation
        if self.augmentation:
            img_norm = random_jitter(
                img_norm, brightness=0.5, contrast=0.5, saturation=0.5, hue=0.2
            )
            img_norm, target = random_rotation(img_norm, target, degrees=10)
        else:
            img_norm = img_norm

        img_norm = color_normalize(img_norm, self.mean, self.std)

        if cid ==3 or cid==7:
            raise NotImplementedError
        meta = {
            "inp": resize(img_orig, 600, 350),
            "folder_name": folder_name,
            "image_name": img_name,
            "index": index,
            "center": 0,
            "scale": 0,
            "pts": pts,
            "tpts": tpts,
            "cid": cid,
            "cam_read_id": cid_read,
            "pid": pose_id,
            "joint_exists": joint_exists,
        }

        return img_norm, target, meta

    def greatest_image_id(self):
        return max([parse_img_name(k[1])[1] for k in self.annotation_key])

    def __len__(self):
        return len(self.annotation_key)
