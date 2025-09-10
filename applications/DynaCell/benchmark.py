"""
This script is a demo script for the DynaCell application.
It loads the ome-zarr 0.4v format, calculates metrics and saves the results as csv files
"""

import datetime
import os
from pathlib import Path

import pandas as pd
import torch
from lightning import LightningModule
from lightning.pytorch.loggers import CSVLogger

from viscy.data.dynacell import DynaCellDatabase, DynaCellDataModule
from viscy.trainer import Trainer
from viscy.utils.logging import ParallelSafeMetricsLogger

# Set float32 matmul precision for better performance on Tensor Cores
torch.set_float32_matmul_precision("high")

# Suppress Lightning warnings for intentional CPU usage
os.environ["SLURM_NTASKS"] = "1"  # Suppress SLURM warning
import warnings
warnings.filterwarnings("ignore", "GPU available but not used")
warnings.filterwarnings("ignore", "The `srun` command is available")


def compute_metrics(
    metrics_module: LightningModule,
    cell_types: list,
    organelles: list,
    infection_conditions: list,
    target_database: pd.DataFrame,
    target_channel_name: str,
    prediction_database: pd.DataFrame,
    prediction_channel_name: str,
    log_output_dir: Path,
    log_name: str = "dynacell_metrics",
    log_version: str = None,
    z_slice: slice = None,
    transforms: list = None,
    num_workers: int = 0,
    use_gpu: bool = False,
):
    """
    Compute DynaCell metrics with optional parallel processing.
    
    This function processes virtual staining metrics at the individual timepoint level,
    enabling efficient parallel computation across multiple positions and timepoints.
    
    Parallel Processing Architecture:
    - Each sample represents one (position, timepoint) combination
    - Workers are distributed samples in round-robin fashion by PyTorch DataLoader
    - With num_workers=4: Worker 0 gets samples [0,4,8...], Worker 1 gets [1,5,9...], etc.
    - Each worker processes different timepoints/positions simultaneously
    - Thread-safe logging prevents race conditions in CSV output
    
    Parameters
    ----------
    metrics_module : LightningModule
        The metrics module to use (e.g., IntensityMetrics())
    cell_types : list
        List of cell types to process (e.g., ["A549"])
    organelles : list
        List of organelles to process (e.g., ["HIST2H2BE"])
    infection_conditions : list
        List of infection conditions to process (e.g., ["Mock", "DENV"])
        Multiple conditions are processed with OR logic in a single call
    target_database : pd.DataFrame
        Database containing target image paths and metadata
    target_channel_name : str
        Channel name in target dataset
    prediction_database : pd.DataFrame
        Database containing prediction image paths and metadata
    prediction_channel_name : str
        Channel name in prediction dataset
    log_output_dir : Path
        Directory for output metrics CSV files
    log_name : str, optional
        Name for metrics logging, by default "dynacell_metrics"
    log_version : str, optional
        Version string for logging, by default None (uses timestamp)
    z_slice : slice, optional
        Z-slice to extract from 3D data, by default None
    transforms : list, optional
        List of data transforms to apply, by default None
    num_workers : int, optional
        Number of workers for parallel data loading, by default 0 (sequential)
        Recommended: 2-4 workers for CPU, 4-8 for GPU
    use_gpu : bool, optional
        Whether to use GPU acceleration, by default False
        GPU provides 10-25x speedup for metrics computation
        
    Notes
    -----
    - GPU acceleration provides massive speedup for metrics computation  
    - batch_size is hardcoded to 1 for compatibility with existing metrics code
    - GPU acceleration works excellently even with batch_size=1
    - Uses ParallelSafeMetricsLogger to prevent race conditions in CSV writing
    - Output CSV includes position_name, dataset, and condition metadata
    
    Returns
    -------
    pd.DataFrame or None
        Metrics DataFrame if CSV file is successfully created, None otherwise
    """
    # Generate timestamp for unique versioning
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    if log_version is None:
        log_version = timestamp

    # Create target database
    target_db = DynaCellDatabase(
        database=target_database,
        cell_types=cell_types,
        organelles=organelles,
        infection_conditions=infection_conditions,
        channel_name=target_channel_name,
        z_slice=z_slice,
    )

    # For segmentation, use same channel for pred and target (self-comparison)
    pred_db = DynaCellDatabase(
        database=prediction_database,
        cell_types=cell_types,
        organelles=organelles,
        infection_conditions=infection_conditions,
        channel_name=prediction_channel_name,
        z_slice=z_slice,
    )

    # Create data module with both databases
    dm = DynaCellDataModule(
        target_database=target_db,
        pred_database=pred_db,
        batch_size=1,  # Hardcoded to 1 for metrics compatibility
        num_workers=num_workers,
        transforms=transforms,
    )
    dm.setup(stage="test")

    # Print dataset configuration summary
    sample = next(iter(dm.test_dataloader()))
    # Determine device and processing info
    device_name = "GPU" if use_gpu and torch.cuda.is_available() else "CPU"
    processing_mode = "Parallel" if num_workers > 0 else "Sequential"
    
    print(f"\n📊 Dataset Configuration:")
    print(f"   • Samples: {len(dm.test_dataset)} total across all positions/timepoints")
    print(f"   • Cell types: {cell_types}")
    print(f"   • Organelles: {organelles}")  
    print(f"   • Infection conditions: {infection_conditions}")
    print(f"   • Sample metadata: {sample['cell_type']}, {sample['organelle']}, {sample['infection_condition']}")

    # Setup logging
    log_output_dir.mkdir(exist_ok=True)
    
    if num_workers > 0:
        logger = ParallelSafeMetricsLogger(save_dir=log_output_dir, name=log_name, version=log_version)
        print(f"\n🚀 Processing Mode: {processing_mode} ({num_workers} workers)")
    else:
        logger = CSVLogger(save_dir=log_output_dir, name=log_name, version=log_version)
        print(f"\n🔄 Processing Mode: {processing_mode} (single-threaded)")
    
    print(f"   • Device: {device_name}")
    print(f"   • Batch size: 1 (hardcoded for metrics compatibility)")
    if use_gpu and torch.cuda.is_available():
        print(f"   • GPU: {torch.cuda.get_device_name()}")

    # Configure trainer based on device preference
    if use_gpu and torch.cuda.is_available():
        accelerator = "gpu"
        precision = "16-mixed"  # Use fp16 on GPU
    else:
        accelerator = "cpu"
        precision = "bf16-mixed"  # Use bf16 for CPU
    
    trainer = Trainer(
        logger=logger,
        accelerator=accelerator,
        devices=1,
        precision=precision,
        num_nodes=1,
        enable_progress_bar=True,
        enable_model_summary=False
    )
    trainer.test(metrics_module, datamodule=dm)
    
    # Finalize logging if using parallel-safe logger
    if hasattr(logger, 'finalize'):
        logger.finalize()

    # Find and report results
    metrics_file = log_output_dir / log_name / log_version / "metrics.csv"
    if metrics_file.exists():
        metrics = pd.read_csv(metrics_file)
        print(f"\n✅ Metrics computation completed successfully!")
        print(f"   • Output file: {metrics_file}")
        print(f"   • Records: {len(metrics)} samples")
        print(f"   • Device: {device_name}")
        print(f"   • Batch size: 1 (hardcoded)")
        print(f"   • Metrics: {[col for col in metrics.columns if col not in ['position', 'time', 'cell_type', 'organelle', 'infection_condition', 'dataset', 'position_name']]}")
        
        # Show infection condition breakdown
        if 'infection_condition' in metrics.columns:
            condition_counts = metrics['infection_condition'].value_counts()
            print(f"   • Conditions: {dict(condition_counts)}")
            
        # Show GPU memory usage if applicable
        if use_gpu and torch.cuda.is_available():
            memory_used = torch.cuda.max_memory_allocated() / 1024**3
            print(f"   • Peak GPU memory: {memory_used:.2f} GB")
    else:
        print(f"❌ Warning: Metrics file not found at {metrics_file}")
        metrics = None

    return metrics
