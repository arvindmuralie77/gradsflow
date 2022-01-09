#  Copyright (c) 2021 GradsFlow. All rights reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
from typing import Dict, List

from loguru import logger
from rich import box
from rich.table import Table

from gradsflow.core.base import BaseTracker, TrackingValues
from gradsflow.utility.common import to_item


class Tracker(BaseTracker):
    """
    Tracks loss, accuracy and model weights during model.fit()
    """

    def __init__(self):
        self.train.metrics = {}
        self.val.metrics = {}
        self.logs: List[Dict] = []

    def __getitem__(self, key: str):
        """
        1. key= `train | val` then return respective `TrackingValues` object
        2. key=`metrics` then return a dictionary of metrics
        3. key=`loss` then return a dictionary of losses
        Args:
            key: train, val, metrics or loss

        Returns:
            `TrackingValues` or a Dictionary
        """
        if key == "train" or key == "val":
            return self.mode(key)
        elif key == "metrics":
            return {"train": self.train_metrics, "val": self.val_metrics}
        elif key == "loss":
            return {"train": self.train_loss, "val": self.val_loss}

        raise KeyError(f"key {key} is not implemented!")

    @property
    def train_loss(self):
        return self.train.loss.avg

    @property
    def val_loss(self):
        return self.val.loss.avg

    @property
    def train_metrics(self):
        return self.train.metrics

    @property
    def val_metrics(self):
        return self.val.metrics

    def mode(self, mode) -> TrackingValues:
        if mode == "train":
            return self.train
        if mode == "val":
            return self.val

        raise KeyError(f"mode {mode} is not implemented!")

    def _append_logs(self, key, value):
        """Tracks value"""
        epoch = self.current_epoch
        data = {"current_epoch": epoch, key: to_item(value)}
        self.logs.append(data)

    def track_loss(self, loss: float, mode: str):
        """Tracks loss by adding to `Tracker.logs` and maintaining average loss in a single Epoch with `TrackingValues`.
        Update `TrackingValues` loss which is called with `TrainEvalCallback` at `*_step_end`.
        Args:
            loss: Step Loss
            mode: can be train | val
        """
        loss = to_item(loss)
        value_tracker = self.mode(mode)
        value_tracker.update_loss(loss)
        key = mode + "/" + "loss"
        self._append_logs(key, loss)

    def track_metrics(self, metric: Dict[str, float], mode: str):
        """Update `TrackingValues` metrics. mode can be train or val"""
        value_tracker = self.mode(mode)

        # Track values that averages with epoch
        value_tracker.update_metrics(metric)

        # _append_logs value for each step in a dict
        for k, v in metric.items():
            k = mode + "/" + k
            self._append_logs(k, v)

    def create_table(self) -> Table:
        headings = ["i", "train/loss"]
        row = [self.current_epoch, self.train_loss]

        if self.val.loss.computed:
            headings.append("val/loss")
            row.append(self.val_loss)

        for metric_name, value in self.train_metrics.items():
            headings.append("train/" + metric_name)
            row.append(value.avg)

        for metric_name, value in self.val_metrics.items():
            headings.append("val/" + metric_name)
            row.append(value.avg)

        row = list(map(lambda x: f"{x: .3f}" if isinstance(x, float) else str(x), row))
        table = Table(*headings, expand=True, box=box.SIMPLE)
        table.add_row(*row)
        return table

    def reset(self):
        """Resets epochs, logs and train & val `TrackingValues`."""
        logger.debug("Reset Tracker")
        self.max_epochs = 0
        self.current_epoch = 0
        self.steps_per_epoch = None
        self.train = TrackingValues()
        self.val = TrackingValues()
        self.logs = []
