import errno
import os
import shutil

from datetime import datetime

from middlewared.client import ejson as json
from middlewared.plugins.zfs_.validation_utils import validate_snapshot_name
from middlewared.schema import Dict, Str, returns
from middlewared.service import accepts, CallError, job, private, Service

from .utils import BACKUP_NAME_PREFIX, UPDATE_BACKUP_PREFIX


class KubernetesService(Service):

    @accepts(
        Str('backup_name', null=True, default=None)
    )
    @returns(Str('backup_name'))
    @job(lock='chart_releases_backup')
    def backup_chart_releases(self, job, backup_name):
        """
        Create a backup of existing chart releases.

        The backup will save helm configuration with history for each chart release and then take a
        snapshot of `ix-applications` dataset.
        """
        self.middleware.call_sync('kubernetes.validate_k8s_setup')
        name = backup_name or datetime.utcnow().strftime('%F_%T')
        k8s_config = self.middleware.call_sync('kubernetes.config')
        if not validate_snapshot_name(f'a@{name}'):
            # The a@ added is just cosmetic as the function requires a complete snapshot name
            # with the dataset name included in it
            raise CallError(f'{name!r} is not a valid snapshot name. It should be a valid ZFS snapshot name')

        snap_name = BACKUP_NAME_PREFIX + name
        if self.middleware.call_sync('zfs.snapshot.query', [['id', '=', f'{k8s_config["dataset"]}@{snap_name}']]):
            raise CallError(f'{snap_name!r} snapshot already exists', errno=errno.EEXIST)

        if name in self.list_backups():
            raise CallError(f'Backup with {name!r} already exists', errno=errno.EEXIST)

        backup_base_dir = os.path.join('/mnt', k8s_config['dataset'], 'backups')
        os.makedirs(backup_base_dir, exist_ok=True)
        backup_dir = os.path.join(backup_base_dir, name)
        os.makedirs(backup_dir)

        job.set_progress(10, 'Basic validation complete')
        chart_releases = self.middleware.call_sync('chart.release.query', [], {'extra': {'retrieve_resources': True}})
        len_chart_releases = len(chart_releases)
        for index, chart_release in enumerate(chart_releases):
            job.set_progress(
                10 + ((index + 1) / len_chart_releases) * 80, f'Backing up {chart_release["name"]}'
            )
            chart_release_backup_path = os.path.join(backup_dir, chart_release['name'])
            os.makedirs(chart_release_backup_path)
            with open(os.path.join(chart_release_backup_path, 'namespace.yaml'), 'w') as f:
                f.write(self.middleware.call_sync('k8s.namespace.export_to_yaml', chart_release['namespace']))

            secrets_dir = os.path.join(chart_release_backup_path, 'secrets')
            os.makedirs(secrets_dir)

            secrets = self.middleware.call_sync(
                'k8s.secret.query', [
                    ['type', 'in', ['helm.sh/release.v1', 'Opaque']],
                    ['metadata.namespace', '=', chart_release['namespace']]
                ]
            )
            # We ignore this keeping in line with helm behaviour where the secret malformed is ignored by helm
            for secret in sorted(filter(lambda d: d.get('data'), secrets), key=lambda d: d['metadata']['name']):
                with open(os.path.join(secrets_dir, secret['metadata']['name']), 'w') as f:
                    f.write(self.middleware.call_sync('k8s.secret.export_to_yaml_internal', secret))

            with open(os.path.join(chart_release_backup_path, 'workloads_replica_counts.json'), 'w') as f:
                f.write(json.dumps(self.middleware.call_sync(
                    'chart.release.get_replica_count_for_resources', chart_release['resources'],
                )))

        job.set_progress(95, 'Taking snapshot of ix-applications')

        self.middleware.call_sync(
            'zettarepl.create_recursive_snapshot_with_exclude', k8s_config['dataset'],
            snap_name, list(self.to_ignore_datasets_on_backup(k8s_config['dataset']))
        )

        job.set_progress(100, f'Backup {name!r} complete')

        return name

    @private
    def to_ignore_datasets_on_backup(self, k8s_dataset):
        return {
            os.path.join(k8s_dataset, ds_name): ds_props
            for ds_name, ds_props in {
                'catalogs': {'mount': True},
                'k3s/kubelet': {'mount': False, 'creation_props': {'mountpoint': 'legacy'}},
            }.items()
        }

    @accepts()
    @returns(Dict('backups', additional_attrs=True))
    def list_backups(self):
        """
        List existing chart releases backups.
        """
        if not self.middleware.call_sync('kubernetes.pool_configured') or self.middleware.call_sync(
            'kubernetes.config'
        )['passthrough_mode']:
            return {}

        k8s_config = self.middleware.call_sync('kubernetes.config')
        backup_base_dir = os.path.join('/mnt', k8s_config['dataset'], 'backups')

        backups = {}
        snapshots = self.middleware.call_sync(
            'zfs.snapshot.query', [['name', '^', f'{k8s_config["dataset"]}@{BACKUP_NAME_PREFIX}']], {'select': ['name']}
        )
        releases_datasets = set(
            ds['id'].split('/', 3)[-1].split('/', 1)[0] for ds in self.middleware.call_sync(
                'zfs.dataset.get_instance', f'{k8s_config["dataset"]}/releases'
            )['children']
        )

        for snapshot in snapshots:
            backup_name = snapshot['name'].split('@', 1)[-1].split(BACKUP_NAME_PREFIX, 1)[-1]
            backup_path = os.path.join(backup_base_dir, backup_name)
            if not os.path.exists(backup_path):
                continue

            backup_data = {
                'name': backup_name,
                'releases': [],
                'snapshot_name': snapshot['name'],
                'created_on': self.middleware.call_sync(
                    'zfs.snapshot.get_instance', snapshot['name']
                )['properties']['creation']['parsed'],
                'backup_path': backup_path,
            }

            for release in filter(lambda r: r in releases_datasets, os.listdir(backup_path)):
                backup_data['releases'].append(release)

            backups[backup_name] = backup_data

        return backups

    @accepts(Str('backup_name'))
    @returns()
    def delete_backup(self, backup_name):
        """
        Delete `backup_name` chart releases backup.
        """
        self.middleware.call_sync('kubernetes.validate_k8s_setup')

        backup = self.middleware.call_sync('kubernetes.list_backups').get(backup_name)
        if not backup:
            raise CallError(f'Backup {backup_name!r} does not exist', errno=errno.ENOENT)

        self.middleware.call_sync('zfs.snapshot.delete', backup['snapshot_name'], {'recursive': True})
        shutil.rmtree(backup['backup_path'], True)

    @private
    async def get_system_update_backup_prefix(self):
        return UPDATE_BACKUP_PREFIX


async def post_system_update_hook(middleware):
    if not (await middleware.call('kubernetes.config'))['dataset']:
        # If k8s is not configured, there is nothing to backup
        return

    backups = [
        v for k, v in (await middleware.call('kubernetes.list_backups')).items()
        if k.startswith(UPDATE_BACKUP_PREFIX)
    ]
    if len(backups) >= 3:
        backups.sort(key=lambda d: d['created_on'])
        while len(backups) >= 3:
            backup = backups.pop(0)
            try:
                await middleware.call('kubernetes.delete_backup', backup['name'])
            except Exception as e:
                middleware.logger.error(
                    'Failed to delete %r chart releases backup: %s', backup['name'], e, exc_info=True
                )
                break

    backup_job = await middleware.call(
        'kubernetes.backup_chart_releases', f'{UPDATE_BACKUP_PREFIX}-{datetime.utcnow().strftime("%F_%T")}'
    )
    await backup_job.wait()
    if backup_job.error:
        middleware.logger.error('Failed to backup chart releases: %s', backup_job.error)


async def setup(middleware):
    middleware.register_hook('update.post_update', post_system_update_hook, sync=True)
