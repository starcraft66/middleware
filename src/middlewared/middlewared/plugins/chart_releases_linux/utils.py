import copy
import enum
import os

from middlewared.plugins.container_runtime_interface.utils import normalize_reference
from middlewared.plugins.kubernetes_linux.utils import NVIDIA_RUNTIME_CLASS_NAME
from middlewared.service import CallError
from middlewared.utils import run as _run


CHART_NAMESPACE_PREFIX = 'ix-'
CONTEXT_KEY_NAME = 'ixChartContext'
RESERVED_NAMES = [
    ('ixCertificates', dict),
    ('ixCertificateAuthorities', dict),
    ('ixExternalInterfacesConfiguration', list),
    ('ixExternalInterfacesConfigurationNames', list),
    ('ixVolumes', list),
    (CONTEXT_KEY_NAME, dict),
]


class Resources(enum.Enum):
    CRONJOB = 'cronjobs'
    DEPLOYMENT = 'deployments'
    JOB = 'jobs'
    POD = 'pods'
    STATEFULSET = 'statefulsets'


def get_action_context(release_name):
    return copy.deepcopy({
        'addNvidiaRuntimeClass': False,
        'nvidiaRuntimeClassName': NVIDIA_RUNTIME_CLASS_NAME,
        'operation': None,
        'isInstall': False,
        'isUpdate': False,
        'isUpgrade': False,
        'isStopped': False,
        'storageClassName': get_storage_class_name(release_name),  # TODO: Remove this usage in next major release
        'upgradeMetadata': {},
        'hasSMBCSI': True,
        'hasNFSCSI': True,
        'smbProvisioner': 'smb.csi.k8s.io',
        'nfsProvisioner': 'nfs.csi.k8s.io',
    })


async def add_context_to_configuration(config, context_dict, middleware, release_name):
    context_dict[CONTEXT_KEY_NAME].update({
        'kubernetes_config': {
            k: v for k, v in (await middleware.call('kubernetes.config')).items()
            if k in ('cluster_cidr', 'service_cidr', 'cluster_dns_ip')
        },
        'addNvidiaRuntimeClass': config.get(CONTEXT_KEY_NAME, {}).get('addNvidiaRuntimeClass', False),
    })
    if 'global' in config:
        config['global'].update(context_dict)
        config.update(context_dict)
    else:
        config.update({
            'global': context_dict,
            **context_dict
        })
    config['release_name'] = release_name
    return config


def get_namespace(release_name):
    return f'{CHART_NAMESPACE_PREFIX}{release_name}'


def get_chart_release_from_namespace(namespace):
    return namespace.split(CHART_NAMESPACE_PREFIX, 1)[-1]


def is_ix_volume_path(path: str, dataset: str) -> bool:
    release_path = os.path.join('/mnt', dataset, 'releases')
    if not path.startswith(release_path):
        return False

    # path -> /mnt/pool/ix-applications/releases/plex/volumes/ix-volumes/
    app_path = path.replace(release_path, '').removeprefix('/').split('/', 1)[0]
    return path.startswith(os.path.join(release_path, app_path, 'volumes/ix_volumes/'))


def normalize_image_tag(tag: str) -> str:
    # This needs to be done as CRI adds registry-1. prefix which it does not
    # do when we query containerd directly
    try:
        complete_tag = normalize_reference(tag)['complete_tag']
    except CallError:
        return tag
    else:
        if complete_tag.startswith('registry-1.docker.io/'):
            return complete_tag.removeprefix('registry-1.')
        else:
            return complete_tag


def default_stats_values() -> dict:
    return {
        'cpu': 0,
        'memory': 0,
        'network': {
            'incoming': 0,
            'outgoing': 0,
        }
    }


def is_ix_namespace(namespace):
    return namespace.startswith(CHART_NAMESPACE_PREFIX)


async def run(*args, **kwargs):
    kwargs['env'] = dict(os.environ, KUBECONFIG='/etc/rancher/k3s/k3s.yaml')
    return await _run(*args, **kwargs)


def get_storage_class_name(release):
    return f'ix-storage-class-{release}'


def get_network_attachment_definition_name(release, count):
    return f'ix-{release}-{count}'


def normalized_port_value(protocol: str, port: int) -> str:
    return '' if ((protocol == 'http' and port == 80) or (protocol == 'https' and port == 443)) else f':{port}'


SCALEABLE_RESOURCES = [
    Resources.DEPLOYMENT,
    Resources.STATEFULSET,
]
SCALE_DOWN_ANNOTATION = {
    'key': 'ix\\.upgrade\\.scale\\.down\\.workload',
    'value': ['true', '1'],
}
