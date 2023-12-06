import enum
import os

from collections import namedtuple


APPS_STATUS = namedtuple('Status', ['status', 'description'])
BACKUP_NAME_PREFIX = 'ix-applications-backup-'
KUBECONFIG_FILE = '/etc/rancher/k3s/k3s.yaml'
KUBERNETES_WORKER_NODE_PASSWORD = 'e3d26cefbdf2f81eff5181e68a02372f'
KUBEROUTER_RULE_PRIORITY = 32764
KUBEROUTER_TABLE_ID = 77
KUBEROUTER_TABLE_NAME = 'kube-router'
MIGRATION_NAMING_SCHEMA = 'ix-app-migrate-%Y-%m-%d_%H-%M'
NODE_NAME = 'ix-truenas'
NVIDIA_RUNTIME_CLASS_NAME = 'nvidia'
UPDATE_BACKUP_PREFIX = 'system-update-'


class Status(enum.Enum):
    PENDING = 'PENDING'
    RUNNING = 'RUNNING'
    INITIALIZING = 'INITIALIZING'
    STOPPING = 'STOPPING'
    STOPPED = 'STOPPED'
    UNCONFIGURED = 'UNCONFIGURED'
    FAILED = 'FAILED'


STATUS_DESCRIPTIONS = {
    Status.PENDING: 'Application(s) state is to be determined yet',
    Status.RUNNING: 'Application(s) are currently running',
    Status.INITIALIZING: 'Application(s) are being initialized',
    Status.STOPPING: 'Application(s) are being stopped',
    Status.STOPPED: 'Application(s) have been stopped',
    Status.UNCONFIGURED: 'Application(s) are not configured',
    Status.FAILED: 'Application(s) have failed to start',
}


def applications_ds_name(pool):
    return os.path.join(pool, 'ix-applications')
