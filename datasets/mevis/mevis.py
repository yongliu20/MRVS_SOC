import json
import torch
from torch.utils.data import Dataset
import torch.distributed as dist
import torchvision.transforms.functional as F
from os import path
from glob import glob
from tqdm import tqdm
from joblib import Parallel, delayed
import multiprocessing
from PIL import Image
import numpy as np
from einops import rearrange
import datasets.transforms as T
from misc import nested_tensor_from_videos_list


class MEVISDataset(Dataset):
    def __init__(self, subset_type: str = 'train', dataset_path: str = '/opt/tiger/ljyaronld/rvosdata/mevis', window_size=12,
                 distributed=False, device=None, **kwargs):
        super(MEVISDataset, self).__init__()
        assert subset_type in ['train', 'test'], "error, unsupported dataset subset type. use 'train' or 'test'."
        dataset_path = '/opt/tiger/ljyaronld/rvosdata/mevis'
        if subset_type == 'test':
            subset_type = 'valid'  # MeViS is tested on its 'validation' subset (see description above)
        self.subset_type = subset_type
        self.window_size = window_size
        num_videos_by_subset = {'train': 1660, 'valid': 140}    #valid_u: 50
        self.videos_dir = path.join(dataset_path, subset_type, 'JPEGImages')
        assert len(glob(path.join(self.videos_dir, '*'))) == num_videos_by_subset[subset_type], \
            f'error: {subset_type} subset is missing one or more frame samples'
        if subset_type == 'train':
            self.mask_annotations_dir = path.join(dataset_path, subset_type, 'Annotations')  # only available for train
            assert len(glob(path.join(self.mask_annotations_dir, '*'))) == num_videos_by_subset[subset_type], \
                f'error: {subset_type} subset is missing one or more mask annotations'
        else:
            self.mask_annotations_dir = None
        self.device = device if device is not None else torch.device('cpu')
        self.samples_list = self.generate_samples_metadata(dataset_path, subset_type, window_size, distributed)
        self.transforms = A2dSentencesTransforms(subset_type, **kwargs)
        self.collator = Collator(subset_type)

    def generate_samples_metadata(self, dataset_path, subset_type, window_size, distributed):
        if subset_type == 'train':
            metadata_file_path = f'./datasets/mevis/train_samples_metadata_win_size_{window_size}.json'
        else:  # validation
            metadata_file_path = f'./datasets/mevis/valid_samples_metadata.json'
        if path.exists(metadata_file_path):
            print(f'loading {subset_type} subset samples metadata...')
            with open(metadata_file_path, 'r') as f:
                samples_list = [tuple(a) for a in tqdm(json.load(f), disable=distributed and dist.get_rank() != 0)]
                # ("vid_name", [frame_ids], {exp: expression, obj_id:xx, exp_id:xx})
                return samples_list
        elif (distributed and dist.get_rank() == 0) or not distributed:
            print(f'creating {subset_type} subset samples metadata...')
            subset_expressions_file_path = path.join(dataset_path, 'meta_expressions', subset_type, 'meta_expressions.json')
            with open(subset_expressions_file_path, 'r') as f:
                subset_expressions_by_video = json.load(f)['videos']

            if subset_type == 'train':
                # generate video samples in parallel (this is required in 'train' mode to avoid long processing times):
                vid_extra_params = (window_size, subset_type, self.mask_annotations_dir, self.device)
                params_by_vid = [(vid_id, vid_data, *vid_extra_params) for vid_id, vid_data in subset_expressions_by_video.items()]
                n_jobs = min(multiprocessing.cpu_count(), 12)
                samples_lists = Parallel(n_jobs)(delayed(self.generate_train_video_samples)(*p) for p in tqdm(params_by_vid))
                samples_list = [s for l in samples_lists for s in l]  # flatten the jobs results lists
            else:  # validation
                # for some reasons the competition's validation expressions dict contains both the validation & test
                # videos. so we simply load the test expressions dict and use it to filter out the test videos from
                # the validation expressions dict:
                test_expressions_file_path = path.join(dataset_path, 'meta_expressions', 'valid', 'meta_expressions.json')
                with open(test_expressions_file_path, 'r') as f:
                    test_expressions_by_video = json.load(f)['videos']
                test_videos = set(test_expressions_by_video.keys())
                # valid_plus_test_videos = set(subset_expressions_by_video.keys())
                # valid_videos = valid_plus_test_videos - test_videos
                valid_videos = test_videos
                subset_expressions_by_video = {k: subset_expressions_by_video[k] for k in valid_videos}
                assert len(subset_expressions_by_video) == 140, 'error: incorrect number of validation expressions'

                samples_list = []
                for vid_id, data in tqdm(subset_expressions_by_video.items()):
                    vid_frames_indices = sorted(data['frames'])
                    #### new
                    chunk = 40
                    if len(vid_frames_indices) > chunk:
                        vid_frames_indices_first = [vid_frames_indices[iii*chunk : (iii+1)*chunk] for iii in range((len(vid_frames_indices) + chunk - 1) // chunk)]
                        # vid_frames_indices_first = vid_frames_indices[:64]
                        # vid_frames_indices_later = vid_frames_indices[64:]
                        for exp_id, exp_dict in data['expressions'].items():
                            exp_dict['exp_id'] = exp_id
                            for jjj in vid_frames_indices_first:
                                samples_list.append((vid_id, jjj, exp_dict))
                    ###
                    else:
                        for exp_id, exp_dict in data['expressions'].items():
                            exp_dict['exp_id'] = exp_id
                            samples_list.append((vid_id, vid_frames_indices, exp_dict))

            with open(metadata_file_path, 'w') as f:
                json.dump(samples_list, f)
        if distributed:
            dist.barrier()
            with open(metadata_file_path, 'r') as f:
                samples_list = [tuple(a) for a in tqdm(json.load(f), disable=distributed and dist.get_rank() != 0)]
        return samples_list

    @staticmethod
    def generate_train_video_samples(vid_id, vid_data, window_size, subset_type, mask_annotations_dir, device):
        vid_frames = sorted(vid_data['frames'])
        vid_windows = [vid_frames[i:i + window_size] for i in range(0, len(vid_frames), window_size)]
        # replace last window with a full window if it is too short:
        if len(vid_windows[-1]) < window_size:
            if len(vid_frames) >= window_size:  # there are enough frames to complete to a full window
                vid_windows[-1] = vid_frames[-window_size:]
            else:  # otherwise, just duplicate the last frame as necessary to complete to a full window
                num_missing_frames = window_size - len(vid_windows[-1])
                missing_frames = num_missing_frames * [vid_windows[-1][-1]]
                vid_windows[-1] = vid_windows[-1] + missing_frames
        samples_list = []
        for exp_id, exp_dict in vid_data['expressions'].items():
            exp_dict['exp_id'] = exp_id
            for window in vid_windows:
                if subset_type == 'train':
                    # if train subset, make sure that the referred object appears in the window, else skip:
                    annotation_paths = [path.join(mask_annotations_dir, vid_id, f'{idx}.png') for idx in window]
                    mask_annotations = [torch.tensor(np.array(Image.open(p)), device=device) for p in annotation_paths]
                    all_object_indices = set().union(*[m.unique().tolist() for m in mask_annotations])
                    allin = True
                    for each_obj_id in exp_dict['obj_id']:
                        if int(each_obj_id) + 1 not in all_object_indices:
                            allin = False
                    # if int(exp_dict['obj_id']) not in all_object_indices:
                    if not allin:
                        continue
                samples_list.append((vid_id, window, exp_dict))
        return samples_list

    @staticmethod
    def bounding_box(img):
        img = img.numpy()
        rows = np.any(img, axis=1)
        cols = np.any(img, axis=0)
        rmin, rmax = np.where(rows)[0][[0, -1]]
        cmin, cmax = np.where(cols)[0][[0, -1]]
        return rmin, rmax, cmin, cmax # y1, y2, x1, x2 
    
    def __getitem__(self, idx):
        video_id, frame_indices, text_query_dict = self.samples_list[idx]
        text_query = text_query_dict['exp']
        text_query = " ".join(text_query.lower().split())  # clean up the text query

        # read the source window frames:
        frame_paths = [path.join(self.videos_dir, video_id, f'{idx}.jpg') for idx in frame_indices]
        source_frames = [Image.open(p) for p in frame_paths]
        original_frame_size = source_frames[0].size[::-1] #[H W]
        h, w = original_frame_size

        if self.subset_type == 'train':
            # TODO
            # with open(path.join('/opt/tiger/ljyaronld/rvosdata/mevis/train', 'meta.json'), 'r') as f:
            #     subset_metas_by_video = json.load(f)['videos']  # only use for obj category
            # read the instance masks:
            annotation_paths = [path.join(self.mask_annotations_dir, video_id, f'{idx}.png') for idx in frame_indices]
            mask_annotations = [torch.tensor(np.array(Image.open(p))) for p in annotation_paths]
            all_object_indices = set().union(*[m.unique().tolist() for m in mask_annotations])
            if 0 in all_object_indices:
                all_object_indices.remove(0)  # remove the background index
            all_object_indices = sorted(list(all_object_indices))
            mask_annotations_by_object = []
            box_annotations_by_object = []
            for obj_id in all_object_indices:
                frames_mask_annotations = []
                frames_box_annotations = []
                for m in mask_annotations:
                    obj_id_mask_annotation = (m == obj_id).to(torch.uint8)
                    if obj_id_mask_annotation.any() > 0:
                        y1, y2, x1, x2 = self.bounding_box(obj_id_mask_annotation)
                        box = torch.tensor([x1, y1, x2, y2]).to(torch.float)
                    else:
                        box = torch.tensor([0, 0, 0, 0]).to(torch.float)
                    frames_mask_annotations.append(obj_id_mask_annotation)
                    frames_box_annotations.append(box)
                
                obj_id_mask_annotations = torch.stack(frames_mask_annotations)
                obj_id_box_annotations = torch.stack(frames_box_annotations) #[o 4]

                obj_id_box_annotations[:, 0::2].clamp_(min=0, max=w)
                obj_id_box_annotations[:, 1::2].clamp_(min=0, max=h)

                # obj_id_mask_annotations = torch.stack([(m == obj_id).to(torch.uint8) for m in mask_annotations])
                box_annotations_by_object.append(obj_id_box_annotations) 
                mask_annotations_by_object.append(obj_id_mask_annotations)
            mask_annotations_by_object = torch.stack(mask_annotations_by_object)
            box_annotations_by_object = torch.stack(box_annotations_by_object)
            
            mask_annotations_by_frame = rearrange(mask_annotations_by_object, 'o t h w -> t o h w')  # o for object
            box_annotations_by_frame = rearrange(box_annotations_by_object, 'o t c -> t o c') #[object t 4]
            # next we get the referred instance index in the list of all the object ids:
            ref_obj_ids = []
            for r_o_id in text_query_dict['obj_id']:
                ref_obj_ids.append(torch.tensor(all_object_indices.index(int(r_o_id+1)), dtype=torch.long))
            # ref_obj_idx = torch.tensor(all_object_indices.index(int(text_query_dict['obj_id'])), dtype=torch.long)

            # category =  subset_metas_by_video[video_id]['objects'][text_query_dict['obj_id']]['category']  #TODO

            # create a target dict for each frame:
            targets = []
            for frame_masks, frames_box in zip(mask_annotations_by_frame, box_annotations_by_frame):
                target_masks = []
                target_boxes = []
                for r_id in ref_obj_ids:
                    target_masks.append(frame_masks[r_id])
                    target_boxes.append(frames_box[r_id])
                target_masks = torch.stack(target_masks, dim=0) # (o, h, w)
                target_boxes = torch.stack(target_boxes, dim=0)
                target_masks = target_masks.any(dim=0).unsqueeze(0)
                # target_boxes = frames_box[ref_obj_ids]  # (o, 4)
                topleft = target_boxes[:, :2]
                bottomright = target_boxes[:, 2:]
                topleft_final = torch.min(topleft, dim=0, keepdim=True).values  # (1, 2)
                bottomright_final = torch.max(bottomright, dim=0, keepdim=True).values  # (1, 2)
                final_boxes = torch.cat([topleft_final, bottomright_final], dim=-1)


                target = {
                          'masks': target_masks,
                          'boxes': final_boxes,
                        #   'masks': frame_masks[ref_obj_idx].unsqueeze(0),
                        #   'boxes': frames_box[ref_obj_idx].unsqueeze(0), #[i 4]
                          # idx in 'masks' of the text referred instance
                          'referred_instance_idx': torch.tensor(0),
                          # whether the referred instance is visible in the frame:
                        #   'is_ref_inst_visible': frame_masks[ref_obj_idx].any(),
                          'is_ref_inst_visible': target_masks.any(),
                          'orig_size': frame_masks.shape[-2:],  # original frame shape without any augmentations
                        #   'labels': torch.tensor([ytvos_category_dict[category]],dtype=torch.long),   # TODO
                          # size with augmentations, will be changed inside transforms if necessary
                          'size': frame_masks.shape[-2:],
                          'iscrowd': torch.zeros(len(frame_masks)),  # for compatibility with DETR COCO transforms
                          }
                targets.append(target)
        else:
            # validation subset has no annotations, so create dummy targets:
            targets = len(source_frames) * [{
                "size": original_frame_size
            }]

        source_frames, targets, text_query = self.transforms(source_frames, targets, text_query)

        if self.subset_type == 'train':
            return source_frames, targets, text_query
        else:  # validation:
            video_metadata = {'video_id': video_id,
                              'frame_indices': frame_indices,
                              'resized_frame_size': source_frames.shape[-2:],
                              'original_frame_size': original_frame_size,
                              'exp_id': text_query_dict['exp_id']}
            return source_frames, video_metadata, targets, text_query
         
    def __len__(self):
        return len(self.samples_list)


class A2dSentencesTransforms:
    def __init__(self, subset_type, horizontal_flip_augmentations, resize_and_crop_augmentations,
                 random_color, train_short_size, train_max_size, eval_short_size, eval_max_size, **kwargs):
        self.h_flip_augmentation = subset_type == 'train' and horizontal_flip_augmentations
        self.random_color = subset_type == 'train' and random_color
        normalize = T.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225])
        scales = [train_short_size]  # no more scales for now due to GPU memory constraints. might be changed later
        self.photometricDistort = T.PhotometricDistort()
        transforms = []
        if resize_and_crop_augmentations:
            if subset_type == 'train':
                transforms.append(T.RandomResize(scales, max_size=train_max_size))
            # elif subset_type == 'test':
            else:
                transforms.append(T.RandomResize([eval_short_size], max_size=eval_max_size)),
        transforms.extend([T.ToTensor(), normalize])
        self.size_transforms = T.Compose(transforms)

    def __call__(self, source_frames, targets, text_query):
        if self.h_flip_augmentation and torch.randn(1) > 0.5:
            source_frames = [F.hflip(f) for f in source_frames]
            for t in targets:
                h, w = t['size']
                t['masks'] = F.hflip(t['masks'])
                boxes = t['boxes'] 
                boxes = boxes[:, [2, 1, 0, 3]] * torch.as_tensor([-1, 1, -1, 1]) + torch.as_tensor([w, 0, w, 0])
                t["boxes"] = boxes
            # Note - is it possible for both 'right' and 'left' to appear together in the same query. hence this fix:
            text_query = text_query.replace('left', '@').replace('right', 'left').replace('@', 'right')
        if self.random_color and torch.randn(1) > 0.5:
            source_frames, targets = self.photometricDistort(source_frames, targets)
        source_frames, targets = list(zip(*[self.size_transforms(f, t) for f, t in zip(source_frames, targets)]))
        source_frames = torch.stack(source_frames)  # [T, 3, H, W]
        return source_frames, targets, text_query


class Collator:
    def __init__(self, subset_type):
        self.subset_type = subset_type

    def __call__(self, batch):
        if self.subset_type == 'train':
            samples, targets, text_queries = list(zip(*batch))
            samples = nested_tensor_from_videos_list(samples)  # [T, B, C, H, W]
            # convert targets to a list of tuples. outer list - time steps, inner tuples - time step batch
            targets = list(zip(*targets))
            batch_dict = {
                'samples': samples,
                'targets': targets,
                'text_queries': text_queries
            }
            return batch_dict
        else:  # validation:
            samples, videos_metadata, targets, text_queries = list(zip(*batch))
            targets = list(zip(*targets))
            samples = nested_tensor_from_videos_list(samples)  # [T, B, C, H, W]
            batch_dict = {
                'samples': samples,
                'videos_metadata': videos_metadata,
                'text_queries': text_queries,
                'targets': targets
            }
            return batch_dict
