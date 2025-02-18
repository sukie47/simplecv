import torch.nn as nn
from torch.utils import checkpoint as cp
from functools import partial
from torchvision.models.resnet import resnet18
from torchvision.models.resnet import resnet34
from torchvision.models.resnet import resnet50
from torchvision.models.resnet import resnet101

from simplecv.interface import CVModule
from simplecv import registry
from simplecv.util import param_util

registry.MODEL.register('resnet18', resnet18)
registry.MODEL.register('resnet34', resnet34)
registry.MODEL.register('resnet50', resnet50)
registry.MODEL.register('resnet101', resnet101)


@registry.MODEL.register('resnet_encoder')
class ResNetEncoder(CVModule):
    def __init__(self,
                 config):
        super(ResNetEncoder, self).__init__(config)
        if all([self.config['output_stride'] != 16,
                self.config['output_stride'] != 32,
                self.config['output_stride'] != 8]):
            raise ValueError('output_stride must be 8, 16 or 32.')

        self.include_conv5 = self.config['include_conv5']
        self.resnet = registry.MODEL[self.config['resnet_type']](pretrained=self.config['pretrained'])
        self.resnet._modules.pop('fc')
        if not self.config['batchnorm_trainable']:
            self._frozen_res_bn()

        self._freeze_at(at=self.config['freeze_at'])

        if self.config['output_stride'] == 16:
            self.resnet.layer4.apply(partial(self._nostride_dilate, dilate=2))
        elif self.config['output_stride'] == 8:
            self.resnet.layer3.apply(partial(self._nostride_dilate, dilate=2))
            self.resnet.layer4.apply(partial(self._nostride_dilate, dilate=4))

    def _frozen_res_bn(self):
        param_util.freeze_modules(self.resnet, nn.BatchNorm2d)
        for m in self.resnet.modules():
            if isinstance(m, nn.BatchNorm2d):
                m.eval()

    def _freeze_at(self, at=2):
        if at >= 1:
            param_util.freeze_params(self.resnet.conv1)
            param_util.freeze_params(self.resnet.bn1)

        if at >= 2:
            param_util.freeze_params(self.resnet.layer1)

        if at >= 3:
            param_util.freeze_params(self.resnet.layer2)

        if at >= 4:
            param_util.freeze_params(self.resnet.layer3)
        if at >= 5:
            param_util.freeze_params(self.resnet.layer4)

    @staticmethod
    def get_function(module):
        def _function(x):
            y = module(x)
            return y

        return _function

    def forward(self, inputs):
        x = inputs
        x = self.resnet.conv1(x)
        x = self.resnet.bn1(x)
        x = self.resnet.relu(x)
        x = self.resnet.maxpool(x)

        # os 4, #layers/outdim: 18,34/64; 50,101,152/256
        if self.with_cp[0] and x.requires_grad:
            c2 = cp.checkpoint(self.get_function(self.resnet.layer1), x)
        else:
            c2 = self.resnet.layer1(x)
        # os 8, #layers/outdim: 18,34/128; 50,101,152/512
        if self.with_cp[1] and c2.requires_grad:
            c3 = cp.checkpoint(self.get_function(self.resnet.layer2), c2)
        else:
            c3 = self.resnet.layer2(c2)
        # os 16, #layers/outdim: 18,34/256; 50,101,152/1024
        if self.with_cp[2] and c3.requires_grad:
            c4 = cp.checkpoint(self.get_function(self.resnet.layer3), c3)
        else:
            c4 = self.resnet.layer3(c3)
        # os 32, #layers/outdim: 18,34/512; 50,101,152/2048
        if self.include_conv5:
            if self.with_cp[3] and c4.requires_grad:
                c5 = cp.checkpoint(self.get_function(self.resnet.layer4), c4)
            else:
                c5 = self.resnet.layer4(c4)
            return [c2, c3, c4, c5]

        return [c2, c3, c4]

    def set_defalut_config(self):
        self.config.update(dict(
            resnet_type='resnet50',
            include_conv5=True,
            batchnorm_trainable=True,
            pretrained=True,
            freeze_at=0,
            # 16 or 32
            output_stride=32,
            with_cp=(False, False, False, False),
        ))

    def _nostride_dilate(self, m, dilate):
        # ref:
        # https://github.com/CSAILVision/semantic-segmentation-pytorch/blob/1235deb1d68a8f3ef87d639b95b2b8e3607eea4c/models/models.py#L256
        classname = m.__class__.__name__
        if classname.find('Conv') != -1:
            # the convolution with stride
            if m.stride == (2, 2):
                m.stride = (1, 1)
                if m.kernel_size == (3, 3):
                    m.dilation = (dilate // 2, dilate // 2)
                    m.padding = (dilate // 2, dilate // 2)
            # other convoluions
            else:
                if m.kernel_size == (3, 3):
                    m.dilation = (dilate, dilate)
                    m.padding = (dilate, dilate)
