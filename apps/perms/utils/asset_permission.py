# coding: utf-8
import time
import pickle
from collections import defaultdict
from functools import reduce

from django.core.cache import cache
from django.db.models import Q
from django.conf import settings

from orgs.utils import current_org
from common.utils import get_logger, timeit, lazyproperty
from common.tree import TreeNode
from assets.utils import TreeService
from ..models import AssetPermission
from ..hands import Node, Asset, SystemUser, User, FavoriteAsset

logger = get_logger(__file__)


__all__ = [
    'ParserNode', 'AssetPermissionUtil',
]


def get_user_permissions(user, include_group=True):
    if include_group:
        groups = user.groups.all()
        arg = Q(users=user) | Q(user_groups__in=groups)
    else:
        arg = Q(users=user)
    return AssetPermission.get_queryset_with_prefetch().filter(arg)


def get_user_group_permissions(user_group):
    return AssetPermission.get_queryset_with_prefetch().filter(
        user_groups=user_group
    )


def get_asset_permissions(asset, include_node=True):
    if include_node:
        nodes = asset.get_all_nodes(flat=True)
        arg = Q(assets=asset) | Q(nodes__in=nodes)
    else:
        arg = Q(assets=asset)
    return AssetPermission.objects.valid().filter(arg)


def get_node_permissions(node):
    return AssetPermission.objects.valid().filter(nodes=node)


def get_system_user_permissions(system_user):
    return AssetPermission.objects.valid().filter(
        system_users=system_user
    )


class AssetPermissionUtilCacheMixin:
    user_tree_cache_key = 'USER_PERM_TREE_{}_{}_{}'
    user_tree_cache_ttl = settings.ASSETS_PERM_CACHE_TIME
    user_tree_cache_enable = settings.ASSETS_PERM_CACHE_ENABLE
    user_tree_map = {}
    cache_policy = '0'
    obj_id = ''
    _filter_id = 'None'

    @property
    def cache_key(self):
        return self.get_cache_key()

    def get_cache_key(self, org_id=None):
        if org_id is None:
            org_id = current_org.org_id()

        key = self.user_tree_cache_key.format(
            org_id, self.obj_id, self._filter_id
        )
        return key

    def expire_user_tree_cache(self):
        cache.delete(self.cache_key)

    @classmethod
    def expire_all_user_tree_cache(cls):
        expire_cache_key = "USER_TREE_EXPIRED_AT"
        latest_expired = cache.get(expire_cache_key, 0)
        now = time.time()
        if now - latest_expired < 60:
            return
        key = cls.user_tree_cache_key.format('*', '1', '1')
        key = key.replace('_1', '')
        cache.delete_pattern(key)
        cache.set(expire_cache_key, now)

    @classmethod
    def expire_org_tree_cache(cls, org_id=None):
        if org_id is None:
            org_id = current_org.org_id()
        key = cls.user_tree_cache_key.format(org_id, '*', '1')
        key = key.replace('_1', '')
        cache.delete_pattern(key)

    def set_user_tree_to_cache(self, user_tree):
        data = pickle.dumps(user_tree)
        cache.set(self.cache_key, data, self.user_tree_cache_ttl)

    def get_user_tree_from_cache(self):
        data = cache.get(self.cache_key)
        if not data:
            return None
        user_tree = pickle.loads(data)
        return user_tree

    @timeit
    def get_user_tree_from_cache_if_need(self):
        if not self.user_tree_cache_enable:
            return None
        if self.cache_policy == '1':
            return self.get_user_tree_from_cache()
        elif self.cache_policy == '2':
            self.expire_user_tree_cache()
            return None
        else:
            return None

    def set_user_tree_to_cache_if_need(self, user_tree):
        if self.cache_policy == '0':
            return
        if not self.user_tree_cache_enable:
            return None
        self.set_user_tree_to_cache(user_tree)


class AssetPermissionUtil(AssetPermissionUtilCacheMixin):
    get_permissions_map = {
        "User": get_user_permissions,
        "UserGroup": get_user_group_permissions,
        "Asset": get_asset_permissions,
        "Node": get_node_permissions,
        "SystemUser": get_system_user_permissions,
    }
    assets_only = (
        'id', 'hostname', 'ip', "platform", "domain_id",
        'comment', 'is_active', 'os', 'org_id'
    )

    def __init__(self, obj=None, cache_policy='0'):
        self.object = obj
        self.cache_policy = cache_policy
        self.obj_id = str(obj.id) if obj else None
        self._permissions = None
        self._filter_id = 'None'  # ?????????filter?????? permission?????????
        self.change_org_if_need()
        self._user_tree = None
        self._user_tree_filter_id = 'None'

        if not isinstance(obj, User):
            self.cache_policy = '0'

    @staticmethod
    def change_org_if_need():
        pass

    @lazyproperty
    def full_tree(self):
        return Node.tree()

    @property
    def permissions(self):
        if self._permissions is not None:
            return self._permissions
        if self.object is None:
            return AssetPermission.objects.none()
        object_cls = self.object.__class__.__name__
        func = self.get_permissions_map[object_cls]
        permissions = func(self.object)
        self._permissions = permissions
        return permissions

    @timeit
    def filter_permissions(self, **filters):
        self.cache_policy = '0'
        self._permissions = self.permissions.filter(**filters)

    @lazyproperty
    def user_tree(self):
        return self.get_user_tree()

    @timeit
    def get_assets_direct(self):
        """
        ??????????????????????????????
        ????????????tree.assets???
        :return:
        {asset.id: {system_user.id: actions, }, }
        """
        assets_ids = self.permissions.values_list('assets', flat=True)
        return Asset.objects.filter(id__in=assets_ids)

    @timeit
    def get_nodes_direct(self):
        """
        ??????????????????????????????
        ?????????????????????tree.nodes???????????????????????????????????????tree.assets???
        :return:
        {node.key: {system_user.id: actions,}, }
        """
        nodes_ids = self.permissions.values_list('nodes', flat=True)
        return Node.objects.filter(id__in=nodes_ids)

    @timeit
    def add_direct_nodes_to_user_tree(self, user_tree):
        """
        ??????????????????????????????????????????, ???full tree???????????????
        """
        nodes_direct_keys = self.permissions \
            .exclude(nodes__isnull=True) \
            .values_list('nodes__key', flat=True) \
            .distinct()
        nodes_direct_keys = list(nodes_direct_keys)
        # ???????????????????????????????????????
        nodes_direct_keys.sort(key=lambda x: len(x))
        for key in nodes_direct_keys:
            # ????????????????????????????????????????????????????????????
            if user_tree.contains(key):
                continue
            # ????????????????????????????????????????????????????????????????????????ROOT???
            parent = self.full_tree.parent(key)
            if not user_tree.contains(parent.identifier):
                parent = user_tree.root_node()
            subtree = self.full_tree.subtree(key)
            user_tree.paste(parent.identifier, subtree, deep=True)

        for node in user_tree.all_nodes_itr():
            assets = list(self.full_tree.assets(node.identifier))
            user_tree.set_assets(node.identifier, assets)

    @timeit
    def add_single_assets_node_to_user_tree(self, user_tree):
        """
        ????????????????????????????????????????????????????????????????????? ????????????????????????????????????
        ???????????????????????????????????????????????????????????????
        """
        # ?????????????????????????????????
        nodes_single_assets = defaultdict(set)
        queryset = self.permissions.exclude(assets__isnull=True) \
            .values_list('assets', 'assets__nodes__key') \
            .distinct()

        for item in queryset:
            nodes_single_assets[item[1]].add(item[0])
        nodes_single_assets.pop(None, None)

        for key in tuple(nodes_single_assets.keys()):
            if user_tree.contains(key):
                nodes_single_assets.pop(key)

        if not nodes_single_assets:
            return

        # ??????????????????ungroup???
        if settings.PERM_SINGLE_ASSET_TO_UNGROUP_NODE:
            node_key = Node.ungrouped_key
            node_value = Node.ungrouped_value
            user_tree.create_node(
                identifier=node_key, tag=node_value,
                parent=user_tree.root,
            )
            assets = set()
            for _assets in nodes_single_assets.values():
                assets.update(set(_assets))
            user_tree.set_assets(node_key, assets)
            return

        # ?????????????????????????????????????????????????????????
        for key, assets in nodes_single_assets.items():
            if not self.full_tree.contains(key):
                continue
            node = self.full_tree.get_node(key, deep=True)
            parent_id = self.full_tree.parent(key).identifier
            parent = user_tree.get_node(parent_id)
            if not parent:
                parent = user_tree.root_node()
            user_tree.add_node(node, parent)
            user_tree.set_assets(node.identifier, assets)

    @timeit
    def parse_user_tree_to_full_tree(self, user_tree):
        """
        ?????????????????????????????????????????????????????????????????????????????????????????????
        ??????????????????????????????????????????
        """
        # ????????????user_tree??????????????????????????????
        root_children = user_tree.children('')
        for child in root_children:
            # print("child: {}".format(child.identifier))
            if child.identifier.isdigit():
                continue
            if child.identifier.startswith('-'):
                continue
            ancestors = self.full_tree.ancestors(
                child.identifier, with_self=False, deep=True,
            )
            # print("Get ancestors: {}".format(len(ancestors)))
            if not ancestors:
                continue
            user_tree.safe_add_ancestors(child, ancestors)

    def add_favorite_node_if_need(self, user_tree):
        if not isinstance(self.object, User):
            return
        node_key = Node.favorite_key
        node_value = Node.favorite_value
        user_tree.create_node(
            identifier=node_key, tag=node_value,
            parent=user_tree.root,
        )
        node = user_tree.get_node(node_key)
        assets_id = FavoriteAsset.get_user_favorite_assets_id(self.object)
        all_valid_assets = user_tree.all_valid_assets(user_tree.root)
        valid_assets_id = set(assets_id) & all_valid_assets
        user_tree.set_assets(node_key, valid_assets_id)
        # ??????????????????????????????????????????
        node.data['all_assets'] = None

    def set_user_tree_to_local(self, user_tree):
        self._user_tree = user_tree
        self._user_tree_filter_id = self._filter_id

    def get_user_tree_from_local(self):
        if self._user_tree and self._user_tree_filter_id == self._filter_id:
            return self._user_tree
        return None

    @timeit
    def get_user_tree(self):
        user_tree = self.get_user_tree_from_cache_if_need()
        if user_tree:
            return user_tree
        user_tree = TreeService()
        full_tree_root = self.full_tree.root_node()
        user_tree.create_node(
            tag=full_tree_root.tag,
            identifier=full_tree_root.identifier
        )
        self.add_direct_nodes_to_user_tree(user_tree)
        self.add_single_assets_node_to_user_tree(user_tree)
        self.parse_user_tree_to_full_tree(user_tree)
        self.add_favorite_node_if_need(user_tree)
        self.set_user_tree_to_cache_if_need(user_tree)
        self.set_user_tree_to_local(user_tree)
        # print(user_tree)
        return user_tree

    # Todo: ?????????????????????????????????????????????
    def get_asset_system_users_id_with_actions(self, asset):
        nodes = asset.get_nodes()
        nodes_keys_related = set()
        for node in nodes:
            ancestor_keys = node.get_ancestor_keys(with_self=True)
            nodes_keys_related.update(set(ancestor_keys))
        kwargs = {"assets": asset}

        if nodes_keys_related:
            kwargs["nodes__key__in"] = nodes_keys_related

        queryset = self.permissions
        if kwargs == 1:
            queryset = queryset.filter(**kwargs)
        elif len(kwargs) > 1:
            kwargs = [{k: v} for k, v in kwargs.items()]
            args = [Q(**kw) for kw in kwargs]
            args = reduce(lambda x, y: x | y, args)
            queryset = queryset.filter(args)
        else:
            queryset = queryset.none()
        asset_protocols = asset.protocols_as_dict.keys()
        values = queryset.filter(system_users__protocol__in=asset_protocols).distinct()\
            .values_list('system_users', 'actions')
        system_users_actions = defaultdict(int)
        for system_user_id, actions in values:
            if None in (system_user_id, actions):
                continue
            for i, action in values:
                system_users_actions[i] |= actions
        return system_users_actions

    def get_permissions_nodes_and_assets(self):
        from assets.models import Node
        permissions = self.permissions
        nodes_keys = permissions.exclude(nodes__isnull=True)\
            .values_list('nodes__key', flat=True)
        assets_ids = permissions.exclude(assets__isnull=True)\
            .values_list('assets', flat=True)
        nodes_keys = set(nodes_keys)
        assets_ids = set(assets_ids)
        nodes_keys = Node.clean_children_keys(nodes_keys)
        return nodes_keys, assets_ids

    @timeit
    def get_assets(self):
        nodes_keys, assets_ids = self.get_permissions_nodes_and_assets()
        queryset = Node.get_nodes_all_assets(
            nodes_keys, extra_assets_ids=assets_ids
        )
        return queryset.valid()

    def get_nodes_assets(self, node, deep=False):
        if deep:
            assets_ids = self.user_tree.all_assets(node.key)
        else:
            assets_ids = self.user_tree.assets(node.key)
        queryset = Asset.objects.filter(id__in=assets_ids)
        return queryset.valid()

    def get_nodes(self):
        return [n.identifier for n in self.user_tree.all_nodes_itr()]

    def get_system_users(self):
        system_users_id = self.permissions.values_list('system_users', flat=True).distinct()
        return SystemUser.objects.filter(id__in=system_users_id)


class ParserNode:
    nodes_only_fields = ("key", "value", "id")
    assets_only_fields = ("hostname", "id", "ip", "protocols", "domain", "org_id")
    system_users_only_fields = (
        "id", "name", "username", "protocol", "priority", "login_mode",
    )

    @staticmethod
    def parse_node_to_tree_node(node):
        name = '{} ({})'.format(node.value, node.assets_amount)
        data = {
            'id': node.key,
            'name': name,
            'title': name,
            'pId': node.parent_key,
            'isParent': True,
            'open': node.is_org_root(),
            'meta': {
                'node': {
                    "id": node.id,
                    "key": node.key,
                    "value": node.value,
                },
                'type': 'node'
            }
        }
        tree_node = TreeNode(**data)
        return tree_node

    @staticmethod
    def parse_asset_to_tree_node(node, asset):
        icon_skin = 'file'
        platform = asset.platform_base.lower()
        if platform == 'windows':
            icon_skin = 'windows'
        elif platform == 'linux':
            icon_skin = 'linux'
        parent_id = node.key if node else ''
        data = {
            'id': str(asset.id),
            'name': asset.hostname,
            'title': asset.ip,
            'pId': parent_id,
            'isParent': False,
            'open': False,
            'iconSkin': icon_skin,
            'nocheck': not asset.has_protocol('ssh'),
            'meta': {
                'type': 'asset',
                'asset': {
                    'id': asset.id,
                    'hostname': asset.hostname,
                    'ip': asset.ip,
                    'protocols': asset.protocols_as_list,
                    'platform': asset.platform_base,
                    'domain': asset.domain_id,
                    'org_name': asset.org_name,
                    'org_id': asset.org_id
                },
            }
        }
        tree_node = TreeNode(**data)
        return tree_node
