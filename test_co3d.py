import os
import torch
import numpy as np
import gzip
import json
import random
from vggt.models.vggt import VGGT
from vggt.utils.rotation import mat_to_quat
from vggt.utils.load_fn import load_and_preprocess_images
from vggt.utils.pose_enc import pose_encoding_to_extri_intri
from vggt.utils.geometry import closed_form_inverse_se3, unproject_depth_map_to_point_map
from ba import run_vggt_with_ba
import argparse


def convert_pt3d_RT_to_opencv(Rot, Trans):
    """
    Convert Point3D extrinsic matrices to OpenCV convention.
    
    Args:
        Rot: 3D rotation matrix in Point3D format
        Trans: 3D translation vector in Point3D format
        
    Returns:
        extri_opencv: 3x4 extrinsic matrix in OpenCV format
    """
    rot_pt3d = np.array(Rot)
    trans_pt3d = np.array(Trans)

    trans_pt3d[:2] *= -1
    rot_pt3d[:, :2] *= -1
    rot_pt3d = rot_pt3d.transpose(1, 0)
    extri_opencv = np.hstack((rot_pt3d, trans_pt3d[:, None]))
    return extri_opencv


def build_pair_index(N, B=1):
    """
    Build indices for all possible pairs of frames.
    
    Args:
        N: Number of frames
        B: Batch size
        
    Returns:
        i1, i2: Indices for all possible pairs
    """
    i1_, i2_ = torch.combinations(torch.arange(N), 2, with_replacement=False).unbind(-1)
    i1, i2 = [(i[None] + torch.arange(B)[:, None] * N).reshape(-1) for i in [i1_, i2_]]
    return i1, i2


def rotation_angle(rot_gt, rot_pred, batch_size=None, eps=1e-15):
    """
    Calculate rotation angle error between ground truth and predicted rotations.
    
    Args:
        rot_gt: Ground truth rotation matrices
        rot_pred: Predicted rotation matrices
        batch_size: Batch size for reshaping the result
        eps: Small value to avoid numerical issues
        
    Returns:
        Rotation angle error in degrees
    """
    q_pred = mat_to_quat(rot_pred)
    q_gt = mat_to_quat(rot_gt)

    loss_q = (1 - (q_pred * q_gt).sum(dim=1) ** 2).clamp(min=eps)
    err_q = torch.arccos(1 - 2 * loss_q)

    rel_rangle_deg = err_q * 180 / np.pi

    if batch_size is not None:
        rel_rangle_deg = rel_rangle_deg.reshape(batch_size, -1)

    return rel_rangle_deg


def translation_angle(tvec_gt, tvec_pred, batch_size=None, ambiguity=True):
    """
    Calculate translation angle error between ground truth and predicted translations.
    
    Args:
        tvec_gt: Ground truth translation vectors
        tvec_pred: Predicted translation vectors
        batch_size: Batch size for reshaping the result
        ambiguity: Whether to handle direction ambiguity
        
    Returns:
        Translation angle error in degrees
    """
    rel_tangle_deg = compare_translation_by_angle(tvec_gt, tvec_pred)
    rel_tangle_deg = rel_tangle_deg * 180.0 / np.pi

    if ambiguity:
        rel_tangle_deg = torch.min(rel_tangle_deg, (180 - rel_tangle_deg).abs())

    if batch_size is not None:
        rel_tangle_deg = rel_tangle_deg.reshape(batch_size, -1)

    return rel_tangle_deg


def compare_translation_by_angle(t_gt, t, eps=1e-15, default_err=1e6):
    """
    Normalize the translation vectors and compute the angle between them.
    
    Args:
        t_gt: Ground truth translation vectors
        t: Predicted translation vectors
        eps: Small value to avoid division by zero
        default_err: Default error value for invalid cases
        
    Returns:
        Angular error between translation vectors in radians
    """
    t_norm = torch.norm(t, dim=1, keepdim=True)
    t = t / (t_norm + eps)

    t_gt_norm = torch.norm(t_gt, dim=1, keepdim=True)
    t_gt = t_gt / (t_gt_norm + eps)

    loss_t = torch.clamp_min(1.0 - torch.sum(t * t_gt, dim=1) ** 2, eps)
    err_t = torch.acos(torch.sqrt(1 - loss_t))

    err_t[torch.isnan(err_t) | torch.isinf(err_t)] = default_err
    return err_t


def calculate_auc(r_error, t_error, max_threshold=30, return_list=False):
    """
    Calculate the Area Under the Curve (AUC) for the given error arrays using PyTorch.

    Args:
        r_error: torch.Tensor representing R error values (Degree)
        t_error: torch.Tensor representing T error values (Degree)
        max_threshold: Maximum threshold value for binning the histogram
        return_list: Whether to return the normalized histogram as well
        
    Returns:
        AUC value, and optionally the normalized histogram
    """
    error_matrix = torch.stack((r_error, t_error), dim=1)
    max_errors, _ = torch.max(error_matrix, dim=1)
    histogram = torch.histc(
        max_errors, bins=max_threshold + 1, min=0, max=max_threshold
    )
    num_pairs = float(max_errors.size(0))
    normalized_histogram = histogram / num_pairs

    if return_list:
        return (
            torch.cumsum(normalized_histogram, dim=0).mean(),
            normalized_histogram,
        )
    return torch.cumsum(normalized_histogram, dim=0).mean()


def calculate_auc_np(r_error, t_error, max_threshold=30):
    """
    Calculate the Area Under the Curve (AUC) for the given error arrays using NumPy.

    Args:
        r_error: numpy array representing R error values (Degree)
        t_error: numpy array representing T error values (Degree)
        max_threshold: Maximum threshold value for binning the histogram
        
    Returns:
        AUC value and the normalized histogram
    """
    error_matrix = np.concatenate((r_error[:, None], t_error[:, None]), axis=1)
    max_errors = np.max(error_matrix, axis=1)
    bins = np.arange(max_threshold + 1)
    histogram, _ = np.histogram(max_errors, bins=bins)
    num_pairs = float(len(max_errors))
    normalized_histogram = histogram.astype(float) / num_pairs
    return np.mean(np.cumsum(normalized_histogram)), normalized_histogram


def se3_to_relative_pose_error(pred_se3, gt_se3, num_frames):
    """
    Compute rotation and translation errors between predicted and ground truth poses.
    
    Args:
        pred_se3: Predicted SE(3) transformations
        gt_se3: Ground truth SE(3) transformations
        num_frames: Number of frames
        
    Returns:
        Rotation and translation angle errors in degrees
    """
    pair_idx_i1, pair_idx_i2 = build_pair_index(num_frames)

    # Compute relative camera poses between pairs
    # We use closed_form_inverse to avoid potential numerical loss by torch.inverse()
    relative_pose_gt = closed_form_inverse_se3(gt_se3[pair_idx_i1]).bmm(
        gt_se3[pair_idx_i2]
    )
    relative_pose_pred = closed_form_inverse_se3(pred_se3[pair_idx_i1]).bmm(
        pred_se3[pair_idx_i2]
    )
    
    # Compute the difference in rotation and translation
    rel_rangle_deg = rotation_angle(
        relative_pose_gt[:, :3, :3], relative_pose_pred[:, :3, :3]
    )
    rel_tangle_deg = translation_angle(
        relative_pose_gt[:, :3, 3], relative_pose_pred[:, :3, 3]
    )

    return rel_rangle_deg, rel_tangle_deg


def align_to_first_camera(camera_poses):
    """
    Align all camera poses to the first camera's coordinate frame.
    
    Args:
        camera_poses: Tensor of shape (N, 4, 4) containing camera poses as SE3 transformations
        
    Returns:
        Tensor of shape (N, 4, 4) containing aligned camera poses
    """
    first_cam_extrinsic_inv = closed_form_inverse_se3(camera_poses[0][None])
    aligned_poses = torch.matmul(camera_poses, first_cam_extrinsic_inv)
    return aligned_poses


def setup_args():
    """Set up command-line arguments for the CO3D evaluation script."""
    parser = argparse.ArgumentParser(description='Test VGGT on CO3D dataset')
    parser.add_argument('--debug', action='store_true', help='Enable debug mode (only test on teddybear category)')
    parser.add_argument('--use_ba', action='store_true', default=False, help='Enable bundle adjustment')
    parser.add_argument('--min_num_images', type=int, default=50, help='Minimum number of images for a sequence')
    parser.add_argument('--num_frames', type=int, default=10, help='Number of frames to use for testing')
    parser.add_argument('--co3d_dir', type=str, required=True, help='Path to CO3D dataset')
    parser.add_argument('--co3d_anno_dir', type=str, required=True, help='Path to CO3D annotations')
    parser.add_argument('--seed', type=int, default=0, help='Random seed for reproducibility')
    return parser.parse_args()


def load_model(device, dtype):
    """
    Load the VGGT model.
    
    Args:
        device: Device to load the model on
        dtype: Data type for model inference
        
    Returns:
        Loaded VGGT model
    """
    print("Initializing and loading VGGT model...")
    model = VGGT()
    _URL = "https://huggingface.co/facebook/VGGT-1B/resolve/main/model.pt"
    model.load_state_dict(torch.hub.load_state_dict_from_url(_URL))
    model.eval()
    model = model.to(device)
    return model


def set_random_seeds(seed):
    """
    Set random seeds for reproducibility.
    
    Args:
        seed: Random seed value
    """
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)


def process_sequence(model, seq_name, seq_data, category, co3d_dir, min_num_images, num_frames, use_ba, device, dtype):
    """
    Process a single sequence and compute pose errors.
    
    Args:
        model: VGGT model
        seq_name: Sequence name
        seq_data: Sequence data
        category: Category name
        co3d_dir: CO3D dataset directory
        min_num_images: Minimum number of images required
        num_frames: Number of frames to sample
        use_ba: Whether to use bundle adjustment
        device: Device to run on
        dtype: Data type for model inference
        
    Returns:
        rError: Rotation errors
        tError: Translation errors
    """
    if len(seq_data) < min_num_images:
        return None, None
    
    metadata = []
    for data in seq_data:
        # Make sure translations are not ridiculous
        if data["T"][0] + data["T"][1] + data["T"][2] > 1e5:
            return None, None

        extri_opencv = convert_pt3d_RT_to_opencv(data["R"], data["T"])
        metadata.append({
            "filepath": data["filepath"],
            "extri": extri_opencv,
        })

    ids = np.random.choice(len(metadata), num_frames, replace=False)
    image_names = [os.path.join(co3d_dir, metadata[i]["filepath"]) for i in ids]
    gt_extri = [np.array(metadata[i]["extri"]) for i in ids]
    gt_extri = np.stack(gt_extri, axis=0)

    images = load_and_preprocess_images(image_names).to(device)

    if use_ba:
        try:
            pred_extrinsic = run_vggt_with_ba(model, images, image_names=image_names, dtype=dtype)
        except Exception as e:
            print(f"BA failed with error: {e}. Falling back to standard VGGT inference.")
            with torch.no_grad():
                with torch.cuda.amp.autocast(dtype=dtype):
                    predictions = model(images)
            with torch.cuda.amp.autocast(dtype=torch.float64):
                extrinsic, intrinsic = pose_encoding_to_extri_intri(predictions["pose_enc"], images.shape[-2:])
                pred_extrinsic = extrinsic[0]
    else:
        with torch.no_grad():
            with torch.cuda.amp.autocast(dtype=dtype):
                predictions = model(images)
        with torch.cuda.amp.autocast(dtype=torch.float64):
            extrinsic, intrinsic = pose_encoding_to_extri_intri(predictions["pose_enc"], images.shape[-2:])
            pred_extrinsic = extrinsic[0]

    with torch.cuda.amp.autocast(dtype=torch.float64):
        gt_extrinsic = torch.from_numpy(gt_extri).to(device)
        add_row = torch.tensor([0, 0, 0, 1], device=device).expand(pred_extrinsic.size(0), 1, 4)

        pred_se3 = torch.cat((pred_extrinsic, add_row), dim=1)
        gt_se3 = torch.cat((gt_extrinsic, add_row), dim=1)

        # Set the coordinate of the first camera as the coordinate of the world
        # NOTE: DO NOT REMOVE THIS UNLESS YOU KNOW WHAT YOU ARE DOING
        # pred_se3 = align_to_first_camera(pred_se3)
        gt_se3 = align_to_first_camera(gt_se3)

        rel_rangle_deg, rel_tangle_deg = se3_to_relative_pose_error(pred_se3, gt_se3, num_frames)
        print(f"{category} sequence {seq_name} Rot Error: {rel_rangle_deg.mean().item():.4f}")
        print(f"{category} sequence {seq_name} Trans Error: {rel_tangle_deg.mean().item():.4f}")
        
        return rel_rangle_deg.cpu().numpy(), rel_tangle_deg.cpu().numpy()


def main():
    """Main function to evaluate VGGT on CO3D dataset."""
    # Parse command-line arguments
    args = setup_args()
    
    # Setup device and data type
    device = "cuda" if torch.cuda.is_available() else "cpu"
    dtype = torch.bfloat16 if torch.cuda.is_available() and torch.cuda.get_device_capability()[0] >= 8 else torch.float16
    
    # Load model
    model = load_model(device, dtype)
    
    # Set random seeds
    set_random_seeds(args.seed)
    
    # Categories to evaluate
    SEEN_CATEGORIES = [
        "apple", "backpack", "banana", "baseballbat", "baseballglove",
        "bench", "bicycle", "bottle", "bowl", "broccoli",
        "cake", "car", "carrot", "cellphone", "chair",
        "cup", "donut", "hairdryer", "handbag", "hydrant",
        "keyboard", "laptop", "microwave", "motorcycle", "mouse",
        "orange", "parkingmeter", "pizza", "plant", "stopsign",
        "teddybear", "toaster", "toilet", "toybus", "toyplane",
        "toytrain", "toytruck", "tv", "umbrella", "vase", "wineglass",
    ]
    
    if args.debug:
        SEEN_CATEGORIES = ["teddybear"]
    
    per_category_results = {}
    
    for category in SEEN_CATEGORIES:
        print(f"Loading annotation for {category} test set")
        annotation_file = os.path.join(args.co3d_anno_dir, f"{category}_test.jgz")
        
        try:
            with gzip.open(annotation_file, "r") as fin:
                annotation = json.loads(fin.read())
        except FileNotFoundError:
            print(f"Annotation file not found for {category}, skipping")
            continue
        
        rError = []
        tError = []
        
        for seq_name, seq_data in annotation.items():
            print("-" * 50)
            
            print(f"Processing {seq_name} for {category} test set")
            if args.debug and not os.path.exists(os.path.join(args.co3d_dir, category, seq_name)):
                print(f"Skipping {seq_name} (not found)")
                continue
            
            seq_rError, seq_tError = process_sequence(
                model, seq_name, seq_data, category, args.co3d_dir, 
                args.min_num_images, args.num_frames, args.use_ba, device, dtype
            )
            
            print("-" * 50)
            
            if seq_rError is not None and seq_tError is not None:
                rError.extend(seq_rError)
                tError.extend(seq_tError)
        
        if not rError:
            print(f"No valid sequences found for {category}, skipping")
            continue
            
        rError = np.array(rError)
        tError = np.array(tError)
        
        Auc_30, _ = calculate_auc_np(rError, tError, max_threshold=30)
        
        print("="*80)
        print(f"AUC of {category} test set: {Auc_30:.4f}")
        print("="*80)
        
        per_category_results[category] = {
            "rError": rError,
            "tError": tError,
            "Auc_30": Auc_30
        }
    
    # Print summary results
    print("\nSummary of AUC results:")
    print("-"*50)
    for category in sorted(per_category_results.keys()):
        print(f"{category:<15}: {per_category_results[category]['Auc_30']:.4f}")
    
    if per_category_results:
        mean_AUC = np.mean([per_category_results[category]["Auc_30"] for category in per_category_results])
        print("-"*50)
        print(f"Mean AUC: {mean_AUC:.4f}")


if __name__ == "__main__":
    main()
