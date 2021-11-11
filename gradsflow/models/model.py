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
import os
from typing import Any, Callable, Dict, List, Optional, Union

import torch
from loguru import logger
from torch import nn
from torchmetrics import Metric

from gradsflow.callbacks import (
    Callback,
    CallbackRunner,
    ProgressCallback,
    TrainEvalCallback,
)
from gradsflow.data import AutoDataset
from gradsflow.data.mixins import DataMixin
from gradsflow.models.base import BaseModel
from gradsflow.models.exceptions import EpochCancel, FitCancel
from gradsflow.models.tracker import Tracker
from gradsflow.utility.common import listify

_METRICS_TYPE = Union[str, Metric, List[Union[str, Metric]], None]
_SCHEDULER_TYPE = Union[str, List[str], Callable, List[Callable]]


class Model(BaseModel, DataMixin):
    """
    Model provide training functionality with `model.fit(...)` inspired from Keras

    Examples:
    ```python
    model = Model(cnn)
    model.compile("crossentropyloss", "adam", learning_rate=1e-3, metrics="accuracy")
    model.fit(autodataset)
    ```

    Args:
        learner: Trainable model
        accelerator_config: HuggingFace Accelerator config
    """

    TEST = os.environ.get("GF_CI", "false").lower() == "true"

    def __init__(
        self,
        learner: Union[nn.Module, Any],
        device: Optional[str] = None,
        use_accelerate: bool = True,
        accelerator_config: dict = None,
    ):
        accelerator_config = accelerator_config or {}
        super().__init__(
            learner=learner,
            device=device,
            use_accelerate=use_accelerate,
            accelerator_config=accelerator_config,
        )
        self.autodataset: Optional[AutoDataset] = None
        self.callback_runner: CallbackRunner = CallbackRunner(self, TrainEvalCallback(self))
        self.disable_auto_optimization = False

    def forward_once(self, x) -> torch.Tensor:
        self.callback_runner.on_forward_start()
        x = self.forward(x)
        self.callback_runner.on_forward_end()
        return x

    def compile(
        self,
        loss: Union[str, nn.Module] = "crossentropyloss",
        optimizer: Union[str, Callable] = "adam",
        schedulers: _SCHEDULER_TYPE = None,
        learning_rate: float = 3e-4,
        metrics: _METRICS_TYPE = None,
        loss_config: Optional[dict] = None,
        optimizer_config: Optional[dict] = None,
        scheduler_config: Optional[List[Dict]] = None,
    ) -> None:
        """
        Compile loss function, optimizer and metrics
        Example:
        ```python
        model = Model(net)

        # without scheduler
        model.compile(loss="crossentropyloss", optimizer="adam", learning_rate=1e-3, metrics="accuracy")

        # with scheduler
        model.compile(loss="crossentropyloss", optimizer="adam", learning_rate=1e-3, metrics="accuracy",
            scheduler="cosineannealinglr", scheduler_config={"T_max":10, "eta_min":0})
        ```

        You can also compile optimizer by passing class as argument-
        ```python
        model.compile(optimizer=torch.optim.SGD, learning_rate=1e-3, optimizer_configs = {"momentum":0.9})
        ```
        To see a list of available losses and metrics-
        ```python
        from gradsflow.models import available_losses, available_metrics
        print(available_losses())
        print(available_metrics())
        ```
        Args:
            loss: name of loss, torch Loss class object or any functional method. See `available_losses()`
            optimizer: optimizer name or `torch.optim.Optimizer` Class
            schedulers: Pytorch scheduler name with config or object.
            learning_rate: defaults to 1e-3
            metrics: list of metrics to calculate. See `available_metrics()`
            loss_config: Dict config if any to pass to loss function
            optimizer_config: Dict config if any to pass to Optimizer
            scheduler_config: list of dictionary configs. It should be equal to number of schedulers.
        """
        loss_config = loss_config or {}
        optimizer_config = optimizer_config or {}
        schedulers = listify(schedulers)
        scheduler_config = [scheduler_config] if isinstance(scheduler_config, dict) else listify(scheduler_config)

        if optimizer:
            optimizer_fn = self._build_optimizer(optimizer)
            optimizer = optimizer_fn(self.learner.parameters(), lr=learning_rate, **optimizer_config)
            self.optimizer = self.prepare_optimizer(optimizer)

        if schedulers:
            self.schedulers = self._build_schedulers(schedulers, self.optimizer, scheduler_config)
        if loss:
            self.loss = self._build_loss(loss, loss_config)
        self.add_metrics(*listify(metrics))
        self._compiled = True

    def calculate_metrics(self, preds, target) -> Dict[str, torch.Tensor]:
        self.metrics.update(preds, target)
        return self.metrics.compute()

    def step(self, batch: Union[List[torch.Tensor], Dict[Any, torch.Tensor]]) -> Dict[str, torch.Tensor]:
        inputs = self.fetch_inputs(batch)
        target = self.fetch_target(batch)
        logits = self.forward_once(inputs)
        loss = self.loss(logits, target)
        return {"loss": loss, "metrics": self.calculate_metrics(logits, target)}

    def train_step(self, batch: Union[List[torch.Tensor], Dict[Any, torch.Tensor]]) -> Dict[str, torch.Tensor]:
        return self.step(batch)

    def val_step(self, batch: Union[List[torch.Tensor], Dict[Any, torch.Tensor]]) -> Dict[str, torch.Tensor]:
        return self.step(batch)

    def train_one_epoch(self, train_dataloader):
        tracker = self.tracker
        steps_per_epoch = tracker.steps_per_epoch

        for step, batch in enumerate(train_dataloader):
            tracker.train.steps = step
            # ----- TRAIN STEP -----
            self.callback_runner.on_train_step_start()
            outputs = self.train_step(batch)
            self.callback_runner.on_train_step_end(data=batch, outputs=outputs)
            if self.TEST:
                break
            if steps_per_epoch and step >= steps_per_epoch:
                break

    def val_one_epoch(self, val_dataloader):
        tracker = self.tracker
        for step, batch in enumerate(val_dataloader):
            tracker.val.steps = step
            # ----- VAL STEP -----
            self.callback_runner.on_val_step_start()
            outputs = self.val_step(batch)
            self.callback_runner.on_val_step_end(data=batch, outputs=outputs)
            if self.TEST:
                break

    def _train_epoch_with_event(self):
        train_dataloader = self.autodataset.train_dataloader
        # ----- TRAIN -----
        self.callback_runner.on_train_epoch_start()
        self.train_one_epoch(train_dataloader)
        self.callback_runner.on_train_epoch_end()

    def _val_epoch_with_event(self):
        autodataset = self.autodataset
        if not autodataset.val_dataloader:
            return
        val_dataloader = self.autodataset.val_dataloader
        # ------ VALIDATE -----
        self.callback_runner.on_val_epoch_start()
        self.val_one_epoch(val_dataloader)
        self.callback_runner.on_val_epoch_end()

    def epoch(self):
        current_epoch, max_epochs = self.tracker.current_epoch, self.tracker.max_epochs

        for epoch in range(current_epoch, max_epochs):
            self.tracker.current_epoch = epoch
            # ----- EPOCH -----
            self.callback_runner.on_epoch_start()
            self._train_epoch_with_event()
            self._val_epoch_with_event()
            self.callback_runner.on_epoch_end()

            if self.TEST:
                break

    def _fit_with_event(self):
        self.callback_runner.on_fit_start()
        self.callback_runner.with_event("epoch", self.epoch, EpochCancel)
        self.callback_runner.on_fit_end()

    def fit(
        self,
        autodataset: AutoDataset,
        max_epochs: int = 1,
        steps_per_epoch: Optional[int] = None,
        callbacks: Optional[Union[List[Callback], Callback, str, List[str]]] = None,
        resume: bool = True,
        show_progress: bool = True,
        progress_kwargs=None,
    ) -> Tracker:
        """
        Similar to Keras model.fit(...) it trains the model for specified epochs and returns Tracker object

        Examples:
        ```python
        autodataset = AutoDataset(train_dataloader, val_dataloader)
        model = Model(cnn)
        model.compile("crossentropyloss", "adam", learning_rate=1e-3, metrics="accuracy")
        model.fit(autodataset)
        ```
        Args:
            autodataset: AutoDataset object encapsulate dataloader and datamodule
            max_epochs: number of epochs to train
            steps_per_epoch: Number of steps trained in a single current_epoch
            callbacks: Callback object or string
            resume: Resume training from the last current_epoch
            show_progress: Enable to show training progress
            progress_kwargs: Arguments for rich.progress

        Returns:
            Tracker object
        """
        self.assert_compiled()
        self.autodataset = autodataset
        self.autodataset.prepare_data(self.accelerator)

        if not resume:
            self.tracker.reset()

        if show_progress:
            self.callback_runner.append(ProgressCallback(self, progress_kwargs))

        callback_list = listify(callbacks)
        for callback in callback_list:
            self.callback_runner.append(callback)

        self.tracker.steps_per_epoch = steps_per_epoch
        self.tracker.max_epochs = max_epochs

        try:
            self.callback_runner.with_event("fit", self._fit_with_event, FitCancel)
        except KeyboardInterrupt:
            logger.info("Keyboard interruption detected")
        finally:
            self.callback_runner.clean()

        return self.tracker
