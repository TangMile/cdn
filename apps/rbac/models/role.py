from django.db import models
from django.utils.translation import ugettext_lazy as _
from common.mixins.models import CommonModelMixin


__all__ = ['Role']


class Role(CommonModelMixin):
    """ 角色: 相当于权限的集合 """
    class TypeChoices(models.TextChoices):
        system = 'system', _('System')
        org = 'org', _('Organization')
        safe = 'safe', _('Safe')

    name = models.CharField(max_length=128, verbose_name=_('Name'))
    # 角色类型: system / org / safe
    type = models.CharField(max_length=128, verbose_name=_('Type'))
    # 权限项
    permissions = models.ManyToManyField('auth.Permission', verbose_name=_('Permission'))
    is_builtin = models.BooleanField(default=False, verbose_name=_('Built-in'))
    comment = models.TextField(null=True, blank=True, verbose_name=_('Comment'))

    class Meta:
        pass

    def __str__(self):
        return self.name
