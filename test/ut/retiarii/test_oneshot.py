import argparse
import torch
import torch.nn.functional as F
import pytorch_lightning as pl
import pytest
from torchvision import transforms
from torchvision.datasets import MNIST
from torch.utils.data import Dataset, RandomSampler

import nni.retiarii.nn.pytorch as nn
from nni.retiarii import strategy, model_wrapper, basic_unit
from nni.retiarii.experiment.pytorch import RetiariiExeConfig, RetiariiExperiment
from nni.retiarii.evaluator.pytorch.lightning import Classification, Regression, DataLoader
from nni.retiarii.nn.pytorch import LayerChoice, InputChoice, ValueChoice


class DepthwiseSeparableConv(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.depthwise = nn.Conv2d(in_ch, in_ch, kernel_size=3, groups=in_ch)
        self.pointwise = nn.Conv2d(in_ch, out_ch, kernel_size=1)

    def forward(self, x):
        return self.pointwise(self.depthwise(x))


@model_wrapper
class SimpleNet(nn.Module):
    def __init__(self, value_choice=True):
        super().__init__()
        self.conv1 = nn.Conv2d(1, 32, 3, 1)
        self.conv2 = LayerChoice([
            nn.Conv2d(32, 64, 3, 1),
            DepthwiseSeparableConv(32, 64)
        ])
        self.dropout1 = LayerChoice([
            nn.Dropout(.25),
            nn.Dropout(.5),
            nn.Dropout(.75)
        ])
        self.dropout2 = nn.Dropout(0.5)
        if value_choice:
            hidden = nn.ValueChoice([32, 64, 128])
        else:
            hidden = 64
        self.fc1 = nn.Linear(9216, hidden)
        self.fc2 = nn.Linear(hidden, 10)
        self.rpfc = nn.Linear(10, 10)
        self.input_ch = InputChoice(2, 1)

    def forward(self, x):
        x = F.relu(self.conv1(x))
        x = F.max_pool2d(self.conv2(x), 2)
        x = torch.flatten(self.dropout1(x), 1)
        x = self.fc1(x)
        x = F.relu(x)
        x = self.dropout2(x)
        x = self.fc2(x)
        x1 = self.rpfc(x)
        x = self.input_ch([x, x1])
        output = F.log_softmax(x, dim=1)
        return output


@model_wrapper
class MultiHeadAttentionNet(nn.Module):
    def __init__(self, head_count):
        super().__init__()
        embed_dim = ValueChoice(candidates=[32, 64])
        self.linear1 = nn.Linear(128, embed_dim)
        self.mhatt = nn.MultiheadAttention(embed_dim, head_count)
        self.linear2 = nn.Linear(embed_dim, 1)

    def forward(self, batch):
        query, key, value = batch
        q, k, v = self.linear1(query), self.linear1(key), self.linear1(value)
        output, _ = self.mhatt(q, k, v, need_weights=False)
        y = self.linear2(output)
        return F.relu(y)


@model_wrapper
class ValueChoiceConvNet(nn.Module):
    def __init__(self):
        super().__init__()
        ch1 = ValueChoice([16, 32])
        kernel = ValueChoice([3, 5])
        self.conv1 = nn.Conv2d(1, ch1, kernel, padding=kernel // 2)
        self.batch_norm = nn.BatchNorm2d(ch1)
        self.conv2 = nn.Conv2d(ch1, 64, 3)
        self.dropout1 = LayerChoice([
            nn.Dropout(.25),
            nn.Dropout(.5),
            nn.Dropout(.75)
        ])
        self.fc = nn.Linear(64, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = self.batch_norm(x)
        x = F.relu(x)
        x = F.max_pool2d(self.conv2(x), 2)
        x = torch.mean(x, (2, 3))
        x = self.fc(x)
        return F.log_softmax(x, dim=1)


@model_wrapper
class RepeatNet(nn.Module):
    def __init__(self):
        super().__init__()
        ch1 = ValueChoice([16, 32])
        kernel = ValueChoice([3, 5])
        self.conv1 = nn.Conv2d(1, ch1, kernel, padding=kernel // 2)
        self.batch_norm = nn.BatchNorm2d(ch1)
        self.conv2 = nn.Conv2d(ch1, 64, 3, padding=1)
        self.dropout1 = LayerChoice([
            nn.Dropout(.25),
            nn.Dropout(.5),
            nn.Dropout(.75)
        ])
        self.fc = nn.Linear(64, 10)
        self.rpfc = nn.Repeat(nn.Linear(10, 10), (1, 4))

    def forward(self, x):
        x = self.conv1(x)
        x = self.batch_norm(x)
        x = F.relu(x)
        x = F.max_pool2d(self.conv2(x), 2)
        x = torch.mean(x, (2, 3))
        x = self.fc(x)
        x = self.rpfc(x)
        return F.log_softmax(x, dim=1)


@basic_unit
class MyOp(nn.Module):
    def __init__(self, some_ch):
        super().__init__()
        self.some_ch = some_ch
        self.batch_norm = nn.BatchNorm2d(some_ch)

    def forward(self, x):
        return self.batch_norm(x)


@model_wrapper
class CustomOpValueChoiceNet(nn.Module):
    def __init__(self):
        super().__init__()
        ch1 = ValueChoice([16, 32])
        kernel = ValueChoice([3, 5])
        self.conv1 = nn.Conv2d(1, ch1, kernel, padding=kernel // 2)
        self.batch_norm = MyOp(ch1)
        self.conv2 = nn.Conv2d(ch1, 64, 3, padding=1)
        self.dropout1 = LayerChoice([
            nn.Dropout(.25),
            nn.Dropout(.5),
            nn.Dropout(.75)
        ])
        self.fc = nn.Linear(64, 10)

    def forward(self, x):
        x = self.conv1(x)
        x = self.batch_norm(x)
        x = F.relu(x)
        x = F.max_pool2d(self.conv2(x), 2)
        x = torch.mean(x, (2, 3))
        x = self.fc(x)
        return F.log_softmax(x, dim=1)


def _mnist_net(type_):
    if type_ == 'simple':
        base_model = SimpleNet(False)
    elif type_ == 'simple_value_choice':
        base_model = SimpleNet()
    elif type_ == 'value_choice':
        base_model = ValueChoiceConvNet()
    elif type_ == 'repeat':
        base_model = RepeatNet()
    elif type_ == 'custom_op':
        base_model = CustomOpValueChoiceNet()
    else:
        raise ValueError(f'Unsupported type: {type_}')
    
    transform = transforms.Compose([transforms.ToTensor(), transforms.Normalize((0.1307,), (0.3081,))])
    train_dataset = MNIST('data/mnist', train=True, download=True, transform=transform)
    train_random_sampler = RandomSampler(train_dataset, True, int(len(train_dataset) / 20))
    train_loader = DataLoader(train_dataset, 64, sampler=train_random_sampler)
    valid_dataset = MNIST('data/mnist', train=False, download=True, transform=transform)
    valid_random_sampler = RandomSampler(valid_dataset, True, int(len(valid_dataset) / 20))
    valid_loader = DataLoader(valid_dataset, 64, sampler=valid_random_sampler)
    evaluator = Classification(train_dataloader=train_loader, val_dataloaders=valid_loader, max_epochs=1)

    return base_model, evaluator


def _multihead_attention_net():
    base_model = MultiHeadAttentionNet(1)

    class AttentionRandDataset(Dataset):
        def __init__(self, data_shape, gt_shape, len) -> None:
            super().__init__()
            self.datashape = data_shape
            self.gtshape = gt_shape
            self.len = len

        def __getitem__(self, index):
            q = torch.rand(self.datashape)
            k = torch.rand(self.datashape)
            v = torch.rand(self.datashape)
            gt = torch.rand(self.gtshape)
            return (q, k, v), gt

        def __len__(self):
            return self.len

    train_set = AttentionRandDataset((1, 128), (1, 1), 1000)
    val_set = AttentionRandDataset((1, 128), (1, 1), 500)
    train_loader = DataLoader(train_set, batch_size=32)
    val_loader = DataLoader(val_set, batch_size=32)

    evaluator = Regression(train_dataloader=train_loader, val_dataloaders=val_loader, max_epochs=1)
    return base_model, evaluator


def _test_strategy(strategy_, support_value_choice=True):
    to_test = [
        # (model, evaluator), support_or_net
        (_mnist_net('simple'), True),
        (_mnist_net('simple_value_choice'), support_value_choice),
        (_mnist_net('value_choice'), support_value_choice),
        (_mnist_net('repeat'), False),      # no strategy supports repeat currently
        (_mnist_net('custom_op'), False),   # this is definitely a NO
        (_multihead_attention_net(), support_value_choice),
    ]

    for (base_model, evaluator), support_or_not in to_test:
        print('Testing:', type(strategy_).__name__, type(base_model).__name__, type(evaluator).__name__, support_or_not)
        experiment = RetiariiExperiment(base_model, evaluator, strategy=strategy_)

        config = RetiariiExeConfig()
        config.execution_engine = 'oneshot'

        if support_or_not:
            experiment.run(config)
            assert isinstance(experiment.export_top_models()[0], dict)
        else:
            with pytest.raises(TypeError, match='not supported'):
                experiment.run(config)


@pytest.mark.skipif(pl.__version__ < '1.0', reason='Incompatible APIs')
def test_darts():
    _test_strategy(strategy.DARTS())


@pytest.mark.skipif(pl.__version__ < '1.0', reason='Incompatible APIs')
def test_proxyless():
    _test_strategy(strategy.Proxyless(), False)


@pytest.mark.skipif(pl.__version__ < '1.0', reason='Incompatible APIs')
def test_enas():
    _test_strategy(strategy.ENAS())


@pytest.mark.skipif(pl.__version__ < '1.0', reason='Incompatible APIs')
def test_random():
    _test_strategy(strategy.RandomOneShot())


@pytest.mark.skipif(pl.__version__ < '1.0', reason='Incompatible APIs')
def test_gumbel_darts():
    _test_strategy(strategy.GumbelDARTS())


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--exp', type=str, default='all', metavar='E',
                        help='experiment to run, default = all')
    args = parser.parse_args()

    if args.exp == 'all':
        test_darts()
        test_proxyless()
        test_enas()
        test_random()
        test_gumbel_darts()
    else:
        globals()[f'test_{args.exp}']()
