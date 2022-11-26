import json
import time
import copy
import os
import pickle
import argparse
import logging

import numpy as np
import wandb
from sklearn.metrics import roc_curve, auc
from sklearn.metrics import precision_recall_fscore_support

import hummingbird.ml
from hummingbird.ml import constants
import torch
from torch import nn

from neumiss import NeuMissBlock, NeuMissDEQBlock


device = torch.device(
    "cuda" if torch.cuda.is_available() else "cpu"
)

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s', datefmt='%m/%d/%Y %H:%M:%S',
                    level=logging.INFO)
logger = logging.getLogger(__name__)


class ArgParser(argparse.ArgumentParser):
    def __init__(self):
        super().__init__()
        self.add_argument(
            "--dataset", type=str, default="pubmed",
            help="Dataset name (pubmed/qian/zbmath/arnetminer)",
        )
        self.add_argument(
            "--dataset_random_seed", type=int, default=1,
            help="S2AND random seed for dataset splits (1/2/3/4/5)",
        )
        self.add_argument(
            "--wandb_sweep_name", type=str, required=True,
            help="Wandb sweep name",
        )
        self.add_argument(
            "--wandb_sweep_method", type=str, default="bayes",
            help="Wandb sweep method (bayes/random/grid)",
        )
        self.add_argument(
            "--wandb_project", type=str, default="missing-values",
            help="Wandb project name",
        )
        self.add_argument(
            "--wandb_entity", type=str, default="dhdhagar",
            help="Wandb entity name",
        )
        self.add_argument(
            "--wandb_sweep_params", type=str, required=True,
            help="Path to wandb sweep parameters JSON",
        )
        self.add_argument(
            "--wandb_sweep_metric_name", type=str, default="dev_auroc",
            help="Wandb sweep metric to optimize (dev_auroc/dev_loss/dev_f1)",
        )
        self.add_argument(
            "--wandb_sweep_metric_goal", type=str, default="maximize",
            help="Wandb sweep metric goal (maximize/minimize)",
        )
        self.add_argument(
            "--wandb_no_early_terminate", action="store_true",
            help="Whether to prevent wandb sweep early terminate or not",
        )
        self.add_argument(
            "--s2and_model", type=str, default="production_model",
            help="S2AND model to use for hummingbird conversion (production_model/full_union_seed_*)",
        )


class NeuMissHB(torch.nn.Module):
    def __init__(self, n_features, neumiss_depth, hb_model):
        super().__init__()
        self.neumiss = NeuMissBlock(n_features=n_features, depth=neumiss_depth)
        self.gbdtnn = hb_model

    def forward(self, x):
        imputed = self.neumiss(x)
        return self.gbdtnn(imputed)


class NeuMissDEQHB(torch.nn.Module):
    def __init__(self, n_features, hb_model):
        super().__init__()
        self.neumiss = NeuMissDEQBlock(n_features=n_features)
        self.gbdtnn = hb_model

    def forward(self, x):
        imputed = self.neumiss(x)
        return self.gbdtnn(imputed)


class NeuMissVanilla(torch.nn.Module):
    def __init__(self, n_features, neumiss_depth, hidden_dim=1024, n_hidden_layers=10, dropout_p=0.1,
                 add_neumiss=True, add_batchnorm=False):
        super().__init__()
        if add_batchnorm:
            self.linear_layer = nn.Sequential(
                *(([NeuMissBlock(n_features=n_features, depth=neumiss_depth)] if add_neumiss else []) +
                  [nn.Linear(n_features, hidden_dim)] +
                  [nn.BatchNorm1d(hidden_dim), nn.ReLU(), nn.Dropout(p=dropout_p),
                   nn.Linear(hidden_dim, hidden_dim)] * n_hidden_layers +
                  [nn.BatchNorm1d(hidden_dim), nn.ReLU(), nn.Dropout(p=dropout_p), nn.Linear(hidden_dim, 1)])
            )
        else:
            self.linear_layer = nn.Sequential(
                *(([NeuMissBlock(n_features=n_features, depth=neumiss_depth)] if add_neumiss else []) +
                  [nn.Linear(n_features, hidden_dim)] +
                  [nn.ReLU(), nn.Dropout(p=dropout_p),
                   nn.Linear(hidden_dim, hidden_dim)] * n_hidden_layers +
                  [nn.ReLU(), nn.Dropout(p=dropout_p), nn.Linear(hidden_dim, 1)])
            )

    def forward(self, x):
        return self.linear_layer(x)


class NeuMissVanillaLeaky(torch.nn.Module):
    def __init__(self, n_features, neumiss_depth, hidden_dim=1024, n_hidden_layers=10, dropout_p=0.1,
                 negative_slope=0.01, add_batchnorm=False, add_neumiss=True):
        super().__init__()
        if add_batchnorm:
            self.linear_layer = nn.Sequential(
                *(([NeuMissBlock(n_features=n_features, depth=neumiss_depth)] if add_neumiss else []) +
                  [nn.Linear(n_features, hidden_dim)] +
                  [nn.BatchNorm1d(hidden_dim), nn.LeakyReLU(negative_slope=negative_slope), nn.Dropout(p=dropout_p),
                   nn.Linear(hidden_dim, hidden_dim)] * n_hidden_layers +
                  [nn.BatchNorm1d(hidden_dim), nn.LeakyReLU(negative_slope=negative_slope), nn.Dropout(p=dropout_p),
                   nn.Linear(hidden_dim, 1)])
            )
        else:
            self.linear_layer = nn.Sequential(
                *(([NeuMissBlock(n_features=n_features, depth=neumiss_depth)] if add_neumiss else []) +
                  [nn.Linear(n_features, hidden_dim)] +
                  [nn.LeakyReLU(negative_slope=negative_slope), nn.Dropout(p=dropout_p),
                   nn.Linear(hidden_dim, hidden_dim)] * n_hidden_layers +
                  [nn.LeakyReLU(negative_slope=negative_slope), nn.Dropout(p=dropout_p), nn.Linear(hidden_dim, 1)])
            )

    def forward(self, x):
        return self.linear_layer(x)


# Function to re-init weights using xavier initialization (tanh); should use He init for relu
def init_weights(model, activation="tanh", vanilla=False):
    if vanilla:
        for p in model.named_parameters():
            if 'weight' in p[0]:
                if len(p[1].data.size()) > 1:
                    if activation == "tanh":
                        torch.nn.init.xavier_uniform_(p[1].data, gain=nn.init.calculate_gain(activation))
                    else:
                        torch.nn.init.kaiming_uniform_(p[1].data, nonlinearity=activation)  # "relu" / "leaky_relu"
            elif 'bias' in p[0]:
                p[1].data.fill_(0.01)
    else:
        for p in model.parameters():
            if len(p.size()) > 0:
                if p.size(-1) == 1:
                    p.data.fill_(0.01)
                else:
                    if activation == "tanh":
                        torch.nn.init.xavier_uniform_(p.data, gain=nn.init.calculate_gain("tanh"))
                    else:
                        torch.nn.init.kaiming_uniform_(p.data, nonlinearity=activation)  # "relu" / "leaky_relu"


# Count parameters in the model
def count_parameters(model): return sum(p.numel() for p in model.parameters() if p.requires_grad)


# Convert the S2 LGBM classifier to torch
def convert_gbdt_to_torch(classifier_model, test_input=None, dropout=0.1,
                          fine_tune=True, force_gemm=False,
                          fine_tune_temp={'train': 1., 'eval': 1., 'requires_grad': False},
                          fine_tune_activation='tanh'):
    extra_config = {}
    if fine_tune:
        extra_config.update({
            constants.FINE_TUNE: True,
            constants.FINE_TUNE_DROPOUT_PROB: dropout
        })
    if force_gemm:
        extra_config[constants.TREE_IMPLEMENTATION] = "gemm"
    extra_config[constants.FINE_TUNE_TEMP] = fine_tune_temp
    extra_config[constants.FINE_TUNE_ACTIVATION] = fine_tune_activation
    humming = hummingbird.ml.convert(classifier_model, "torch", test_input=test_input, extra_config=extra_config)
    return humming.model


# Get data tensors and optionally convert NANs
def get_tensors(X_train, y_train, X_val, y_val, X_test, y_test, convert_nan=True, nan_val=-1):
    X_train_tensor = torch.tensor(X_train)
    y_train_tensor = torch.tensor(y_train)

    X_val_tensor = torch.tensor(X_val)
    y_val_tensor = torch.tensor(y_val)

    X_test_tensor = torch.tensor(X_test)
    y_test_tensor = torch.tensor(y_test)

    if convert_nan:
        X_train_tensor = torch.nan_to_num(X_train_tensor, nan_val)
        X_val_tensor = torch.nan_to_num(X_val_tensor, nan_val)
        X_test_tensor = torch.nan_to_num(X_test_tensor, nan_val)

    return X_train_tensor, y_train_tensor, X_val_tensor, y_val_tensor, X_test_tensor, y_test_tensor


def predict_proba(model, x):
    if model.__class__ in needs_sigmoid:
        return torch.sigmoid(model(x.type(torch.float)))
    else:
        return model(x)[1][:, 1]


def evaluate(model, x, output, mode="macro", return_pred_only=False,
             batch_size=None, overfit_one_batch=False, loss_fn=None, pos_weight=None):
    if batch_size is None:
        if model.__class__ in needs_sigmoid:
            y_prob = torch.sigmoid(model(x.type(torch.float))).cpu().numpy()
        else:
            y_prob = model(x)[1][:, 1].cpu().numpy()
    else:
        y_prob = []
        for i in range(0, batch_size if overfit_one_batch else len(x), batch_size):
            if model.__class__ in needs_sigmoid:
                _y_prob = torch.sigmoid(model(x[i:i + batch_size].type(torch.float).to(device))).cpu().numpy()
            else:
                _y_prob = model(x[i:i + batch_size].to(device))[1][:, 1].cpu().numpy()
            y_prob.append(_y_prob)
        y_prob = np.concatenate(y_prob, axis=0).flatten()

    if return_pred_only:
        return y_prob

    y = output.numpy()

    if batch_size is not None and overfit_one_batch:
        y = y[:batch_size]

    fpr, tpr, _ = roc_curve(y, y_prob)
    roc_auc = auc(fpr, tpr)
    thresh_for_f1 = 0.5
    pr, rc, f1, _ = precision_recall_fscore_support(y, y_prob > thresh_for_f1, beta=1.0, average=mode)

    if loss_fn is not None:
        if 'BCELoss' in loss_fn.__class__.__name__:
            if pos_weight is None:
                loss_fn.weight = None
            else:
                loss_weight = (pos_weight * torch.tensor(y).type(torch.float))
                loss_weight[loss_weight == 0] = 1.
                loss_fn.weight = loss_weight
        loss = loss_fn(torch.tensor(y_prob).type(torch.float).view_as(torch.tensor(y)),
                       torch.tensor(y).type(torch.float))
        return roc_auc, np.round(f1, 3), loss.item()

    return roc_auc, np.round(f1, 3)


def train(dataset_name="pubmed", dataset_random_seed=1, verbose=False):
    # Default hyperparameters
    hyperparams = {
        # Dataset
        "dataset": dataset_name,
        "dataset_random_seed": dataset_random_seed,
        # Training config
        "lr": 1e-4,
        "n_epochs": 200,
        "weighted_loss": True,
        "batch_size": 10000,
        "use_lr_scheduler": True,
        "lr_factor": 0.6,
        "lr_min": 1e-6,
        "lr_scheduler_patience": 10,
        "weight_decay": 0.,
        "dev_opt_metric": 'auroc',
        "overfit_one_batch": False,
        "convert_nan": False,
        "nan_value": -1,
        # Model config
        "hb_model": False,
        "hb_temp": 1e-8,
        "hb_activation": 'tanh',
        "neumiss_deq": False,
        "neumiss_depth": 20,
        "vanilla_hidden_dim": 1024,
        "vanilla_n_hidden_layers": 1,
        "vanilla_dropout": 0.,
        "vanilla_batchnorm": True,
        "vanilla_activation": "leaky_relu",
        "reinit_model": False
    }

    # Start wandb run
    with wandb.init(config=hyperparams):
        hyp = wandb.config

        # Load data
        out_dir = os.path.join("data", hyp["dataset"], "clf_random_splits")
        out_fname = os.path.join(out_dir, f"splits_rand{hyp['dataset_random_seed']}.pkl")
        with open(out_fname, 'rb') as out_fh:
            splits = pickle.load(out_fh)

        # Get tensors
        all_tensors = get_tensors(splits['X_train'], splits['y_train'], splits['X_val'], splits['y_val'],
                                  splits['X_test'], splits['y_test'], convert_nan=hyp['convert_nan'],
                                  nan_val=hyp['nan_value'])
        X_train_tensor, y_train_tensor, X_val_tensor, y_val_tensor, X_test_tensor, y_test_tensor = all_tensors
        del splits

        # Create model
        if hyp['hb_model']:
            # Load S2AND production classifier and convert to NN
            with open("data/production_model.pickle", "rb") as _pkl_file:
                prod_model = pickle.load(_pkl_file)
                if type(prod_model) == dict:
                    prod_model = prod_model["clusterer"]
                s2and_classifier = prod_model.classifier
            hb_model = convert_gbdt_to_torch(s2and_classifier, fine_tune=True, force_gemm=True,
                                             fine_tune_temp={'train': hyp['hb_temp'], 'eval': hyp['hb_temp'],
                                                             'requires_grad': False},
                                             fine_tune_activation=hyp['hb_activation'])
            if hyp['convert_nan']:
                model = hb_model
            else:
                if hyp['neumiss_deq']:
                    model = NeuMissDEQHB(n_features=X_train_tensor.shape[1], hb_model=hb_model)
                else:
                    model = NeuMissHB(n_features=X_train_tensor.shape[1], neumiss_depth=hyp['neumiss_depth'],
                                      hb_model=hb_model)
        else:
            if hyp['vanilla_activation'] == 'relu':
                model = NeuMissVanilla(n_features=X_train_tensor.shape[1], neumiss_depth=hyp['neumiss_depth'],
                                       hidden_dim=hyp['vanilla_hidden_dim'],
                                       n_hidden_layers=hyp['vanilla_n_hidden_layers'],
                                       dropout_p=hyp['vanilla_dropout'], add_neumiss=not hyp['convert_nan'],
                                       add_batchnorm=hyp['vanilla_batchnorm'])
            elif hyp['vanilla_activation'] == 'leaky_relu':
                model = NeuMissVanillaLeaky(n_features=X_train_tensor.shape[1], neumiss_depth=hyp['neumiss_depth'],
                                            hidden_dim=hyp['vanilla_hidden_dim'],
                                            n_hidden_layers=hyp['vanilla_n_hidden_layers'],
                                            dropout_p=hyp['vanilla_dropout'],
                                            add_neumiss=not hyp['convert_nan'],
                                            add_batchnorm=hyp['vanilla_batchnorm'])

        if hyp['reinit_model']:
            if hyp['hb_model']:
                init_weights(model.gbdtnn, activation=hyp['hb_activation'], vanilla=False)
            else:
                init_weights(model.linear_layer, activation=hyp['vanilla_activation'], vanilla=True)

        # Training code
        batch_size = hyp['batch_size']
        weighted_loss = hyp['weighted_loss']
        overfit_one_batch = hyp['overfit_one_batch']
        dev_opt_metric = hyp['dev_opt_metric']
        n_epochs = hyp['n_epochs']
        use_lr_scheduler = hyp['use_lr_scheduler']

        model.to(device)
        wandb.watch(model)

        y_train_tensor = y_train_tensor.float()  # Converting to keep output and prediction dtypes consistent

        pos_weight = None
        if weighted_loss:
            if overfit_one_batch:
                pos_weight = (batch_size - y_train_tensor[:batch_size].sum()) / y_train_tensor[:batch_size].sum()
            else:
                pos_weight = (len(y_train_tensor) - y_train_tensor.sum()) / y_train_tensor.sum()

        if verbose:
            logger.info(f"Loss function pos_weight={pos_weight}")
        loss_fn = torch.nn.BCEWithLogitsLoss(pos_weight=pos_weight) if model.__class__ in needs_sigmoid else \
            torch.nn.BCELoss()

        group_no_wd, group_wd = [], []
        for name, param in model.named_parameters():
            if '.mu' in name:
                group_no_wd.append(param)
            else:
                group_wd.append(param)
        optimizer = torch.optim.AdamW([{'params': group_wd, 'weight_decay': hyp['weight_decay']},
                                       {'params': group_no_wd, 'weight_decay': 0}], lr=hyp['lr'])
        scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(optimizer,
                                                               mode='min',
                                                               factor=hyp['lr_factor'],
                                                               min_lr=hyp['lr_min'],
                                                               patience=hyp['lr_scheduler_patience'],
                                                               verbose=verbose)

        if verbose:
            logger.info("Training:")
        with torch.no_grad():
            model.eval()
            init_eval_train = evaluate(model, X_train_tensor, y_train_tensor,
                                       batch_size=batch_size, overfit_one_batch=overfit_one_batch)
            init_eval_dev = evaluate(model, X_val_tensor.to(device), y_val_tensor)
            init_eval_test = evaluate(model, X_test_tensor.to(device), y_test_tensor)

            if verbose:
                logger.info(f"Initial model evaluation:")
                logger.info("Train AUROC, F1:", init_eval_train)
                logger.info("Dev AUROC, F1:", init_eval_dev)
                logger.info("Test AUROC, F1:", init_eval_test)

            wandb.log({
                'train_auroc': init_eval_train[0],
                'train_f1': init_eval_train[1],
                'dev_auroc': init_eval_dev[0],
                'dev_f1': init_eval_dev[1],
                'test_auroc': init_eval_test[0],
                'test_f1': init_eval_test[1]})

        if verbose:
            logger.info(f"Dev metric to optimize: {dev_opt_metric}")

        best_model_on_dev = None
        best_metric = -1.
        best_dev_f1 = -1.
        best_dev_auroc = -1.
        best_epoch = -1

        model.train()

        start_time = time.time()
        for i in range(n_epochs):  # epoch
            running_loss = []
            wandb.log({'epoch': i + 1})

            for j in range(0, batch_size if overfit_one_batch else len(X_train_tensor), batch_size):
                X_batch = X_train_tensor[j:j + batch_size].to(device)
                y_batch = y_train_tensor[j:j + batch_size].to(device)

                optimizer.zero_grad()
                y_ = predict_proba(model, X_batch)
                assert y_.requires_grad

                if weighted_loss and 'BCELoss' in loss_fn.__class__.__name__:
                    weights = (pos_weight * y_batch)
                    weights[weights == 0] = 1.
                    loss_fn.weight = weights
                loss = loss_fn(y_.view_as(y_batch), y_batch)
                running_loss.append(loss.item())
                loss.backward()
                optimizer.step()

                # Print batch loss
                if verbose:
                    logger.info("\tBatch", f"[{j}:{j + batch_size}]", ":", running_loss[-1])
                wandb.log({'train_loss_batch': running_loss[-1]})

            # Print epoch validation accuracy
            with torch.no_grad():
                model.eval()
                dev_auroc_f1_loss = evaluate(model, X_val_tensor.to(device), y_val_tensor,
                                             loss_fn=loss_fn, pos_weight=pos_weight)
                if verbose:
                    logger.info("Epoch", i + 1, ":", "Dev AUROC,F1,loss:", dev_auroc_f1_loss)
                if dev_auroc_f1_loss[metric_to_idx[dev_opt_metric]] > best_metric:
                    if verbose:
                        logger.info(f"New best dev {dev_opt_metric}; storing model")
                    best_epoch = i
                    best_metric = dev_auroc_f1_loss[metric_to_idx[dev_opt_metric]]
                    best_dev_f1 = dev_auroc_f1_loss[1]
                    best_dev_auroc = dev_auroc_f1_loss[0]
                    best_model_on_dev = copy.deepcopy(model)
                if overfit_one_batch:
                    train_auroc_f1 = evaluate(model, X_batch.to(device), y_batch.cpu())
            model.train()

            wandb.log({
                'train_loss_epoch': np.mean(running_loss),
                'dev_auroc': dev_auroc_f1_loss[0],
                'dev_f1': dev_auroc_f1_loss[1],
                'dev_loss': dev_auroc_f1_loss[2],
            })
            if overfit_one_batch:
                wandb.log({'train_auroc': train_auroc_f1[0],
                           'train_f1': train_auroc_f1[1]})

            # Update lr schedule
            if use_lr_scheduler:
                scheduler.step(dev_auroc_f1_loss[2])  # running_loss

        end_time = time.time()

        with torch.no_grad():
            best_model_on_dev.eval()
            if verbose:
                logger.info(f"Initial model evaluation:")
                logger.info("Train AUROC, F1:", init_eval_train)
                logger.info("Dev AUROC, F1:", init_eval_dev)
                logger.info("Test AUROC, F1:", init_eval_test)

            best_eval_train = evaluate(best_model_on_dev, X_train_tensor, y_train_tensor,
                                       batch_size=batch_size, overfit_one_batch=overfit_one_batch)
            best_eval_dev = (best_dev_auroc, best_dev_f1)
            best_eval_test = evaluate(best_model_on_dev, X_test_tensor.to(device), y_test_tensor)
            if verbose:
                logger.info(f"Best dev eval on Epoch {best_epoch}:")
                logger.info("Train AUROC, F1:", best_eval_train)
                logger.info("Dev AUROC, F1:", best_eval_dev)
                logger.info("Test AUROC, F1:", best_eval_test)
                logger.info(f"Time taken: {end_time - start_time}")
            wandb.log({
                'best_train_auroc': best_eval_train[0],
                'best_train_f1': best_eval_train[1],
                'best_dev_auroc': best_eval_dev[0],
                'best_dev_f1': best_eval_dev[1],
                'best_test_auroc': best_eval_test[0],
                'best_test_f1': best_eval_test[1]
            })

            model.eval()
            final_eval_train = evaluate(model, X_train_tensor, y_train_tensor,
                                        batch_size=batch_size, overfit_one_batch=overfit_one_batch)
            final_eval_dev = evaluate(model, X_val_tensor.to(device), y_val_tensor)
            final_eval_test = evaluate(model, X_test_tensor.to(device), y_test_tensor)
            if verbose:
                logger.info(f"Final model eval on Epoch {n_epochs}:")
                logger.info("Train AUROC, F1:", final_eval_train)
                logger.info("Dev AUROC, F1:", final_eval_dev)
                logger.info("Test AUROC, F1:", final_eval_test)
            wandb.log({
                'train_auroc': final_eval_train[0],
                'train_f1': final_eval_train[1],
                'dev_auroc': final_eval_dev[0],
                'dev_f1': final_eval_dev[1],
                'test_auroc': final_eval_test[0],
                'test_f1': final_eval_test[1]
            })


needs_sigmoid = [NeuMissVanilla, NeuMissVanillaLeaky]
metric_to_idx = {'auroc': 0, 'f1': 1}


if __name__ == '__main__':
    parser = ArgParser()
    args = parser.parse_args().__dict__
    logger.info("Script arguments:")
    logger.info(args)
    logger.info(f"Using device={device}")

    wandb.login()

    with open(args['wandb_sweep_params'], 'r') as fh:
        sweep_params = json.load(fh)

    sweep_config = {
        'method': args['wandb_sweep_method'],
        'name': args['wandb_sweep_name'],
        'metric': {
            'name': args['wandb_sweep_metric_name'],
            'goal': args['wandb_sweep_metric_goal'],
        },
        'parameters': sweep_params,
    }
    if not args['wandb_no_early_terminate']:
        sweep_config.update({
            'early_terminate': {
                'type': 'hyperband',
                'min_iter': 40
            }
        })

    # Init sweep
    sweep_id = wandb.sweep(sweep=sweep_config,
                           project=args['wandb_project'],
                           entity=args['wandb_entity'])
    # Start sweep job
    wandb.agent(sweep_id, function=lambda: train(dataset_name=args['dataset'],
                                                 dataset_random_seed=args['dataset_random_seed']))

    # Example sweep parameters:
    # {
    #     # Training
    #     'lr': {'max': 1e-2, 'min': 1e-6},
    #     'weighted_loss': {'values': [True, False]},
    #     'weight_decay': {'values': [1e-1, 1e-2, 1e-3, 0]},
    #     'dev_opt_metric': {'values': ['auroc', 'f1']},
    #     "convert_nan": {'value': False},
    #     "nan_value": {'value': -1},
    #     # Model
    #     "hb_model": {'value': False},
    #     #         "hb_temp": {'values': [1., 1e-1, 1e-2, 1e-4, 1e-6, 1e-8]},
    #     #         "hb_activation": {'value': 'tanh'},
    #     "neumiss_deq": {'value': False},
    #     "neumiss_depth": {'values': [5, 10, 20]},
    #     "vanilla_hidden_dim": {'values': [128, 256, 512, 1024, 2048]},
    #     "vanilla_n_hidden_layers": {'values': [1, 2, 3]},
    #     "vanilla_dropout": {'values': [0., 0.1, 0.2, 0.4]},
    #     "vanilla_batchnorm": {'values': [True, False]},
    #     "reinit_model": {'values': [False, True]}
    # }
