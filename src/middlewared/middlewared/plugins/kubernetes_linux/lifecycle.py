import asyncio
import errno
import json
import os
import shutil
import typing
import uuid

from datetime import datetime

from middlewared.schema import accepts, Dict, returns, Str
from middlewared.service import CallError, private, Service
from middlewared.utils import run

from .k8s.config import reinitialize_config
from .utils import APPS_STATUS, Status, STATUS_DESCRIPTIONS


START_LOCK = asyncio.Lock()


class KubernetesService(Service):

    STATUS = APPS_STATUS(Status.PENDING, STATUS_DESCRIPTIONS[Status.PENDING])

    @private
    async def post_start(self):
        reinitialize_config()
        async with START_LOCK:
            return await self.post_start_impl()

    @private
    async def post_start_impl(self):
        try:
            timeout = 60
            while timeout > 0:
                node_config = await self.middleware.call('k8s.node.config')
                if node_config['node_configured']:
                    break
                else:
                    await asyncio.sleep(2)
                    timeout -= 2

            if not node_config['node_configured']:
                raise CallError(f'Unable to configure node: {node_config["error"]}')
            await self.post_start_internal()
            await self.add_iptables_rules()
        except Exception as e:
            await self.set_status(Status.FAILED.value, str(e))
            await self.middleware.call('alert.oneshot_create', 'ApplicationsStartFailed', {'error': str(e)})
            raise
        else:
            self.middleware.create_task(self.middleware.call('k8s.event.setup_k8s_events'))
            await self.middleware.call('chart.release.refresh_events_state')
            await self.middleware.call('alert.oneshot_delete', 'ApplicationsStartFailed', None)
            await self.set_status(Status.RUNNING.value)
            self.middleware.create_task(self.redeploy_chart_releases_consuming_outdated_certs())

    @private
    async def add_iptables_rules(self):
        for rule in await self.iptable_rules():
            cp = await run(['iptables', '-A'] + rule, check=False)
            if cp.returncode:
                self.logger.error(
                    'Failed to append %r iptable rule to isolate kubernetes: %r',
                    ', '.join(rule), cp.stderr.decode(errors='ignore')
                )
                # If adding first rule fails for whatever reason, we won't be adding the second one
                break

    @private
    async def remove_iptables_rules(self):
        for rule in reversed(await self.iptable_rules()):
            cp = await run(['iptables', '-D'] + rule, check=False)
            if cp.returncode:
                self.logger.error(
                    'Failed to delete %r iptable rule: %r', ', '.join(rule), cp.stderr.decode(errors='ignore')
                )

    @private
    async def redeploy_chart_releases_consuming_outdated_certs(self):
        return await self.middleware.call(
            'core.bulk', 'chart.release.update', [
                [r, {'values': {}}] for r in await self.middleware.call(
                    'chart.release.get_chart_releases_consuming_outdated_certs'
                )
            ]
        )

    @private
    async def iptable_rules(self):
        config = await self.middleware.call('kubernetes.config')
        node_ip = await self.middleware.call('kubernetes.node_ip')
        if node_ip in ('0.0.0.0', '::') or config['passthrough_mode']:
            # This shouldn't happen but if it does, we don't add iptables in this case
            # Even if user selects 0.0.0.0, k8s is going to auto select a node ip in this case
            return []

        # https://unix.stackexchange.com/questions/591113/iptables-inserts-duplicate-
        # rules-when-name-localhost-is-used-instead-of-127-0-0
        # We don't use localhost name directly because it adds duplicate entries
        return [
            [
                'INPUT', '-p', 'tcp', '-s', f'{node_ip},127.0.0.1', '--dport', '6443', '-j', 'ACCEPT', '-m', 'comment',
                '--comment', 'iX Custom Rule to allow access to k8s cluster from internal TrueNAS connections',
                '--wait'
            ],
            [
                'INPUT', '-p', 'tcp', '--dport', '6443', '-j', 'DROP', '-m', 'comment', '--comment',
                'iX Custom Rule to drop connection requests to k8s cluster from external sources',
                '--wait'
            ],
        ]

    @private
    async def ensure_k8s_crd_are_available(self):
        retries = 5
        required_crds = [
            'network-attachment-definitions.k8s.cni.cncf.io',
        ]
        while len(
            await self.middleware.call('k8s.crd.query', [['metadata.name', 'in', required_crds]])
        ) < len(required_crds) and retries:
            await asyncio.sleep(5)
            retries -= 1

    @private
    async def post_start_internal(self):
        await self.middleware.call('k8s.node.add_taints', [{'key': 'ix-svc-start', 'effect': 'NoExecute'}])
        await self.middleware.call('k8s.cni.setup_cni')
        await self.middleware.call('k8s.gpu.setup')
        try:
            await self.ensure_k8s_crd_are_available()
            await self.middleware.call('k8s.storage_class.setup_default_storage_class')
            await self.middleware.call('k8s.zfs.snapshotclass.setup_default_snapshot_class')
        except Exception as e:
            raise CallError(f'Failed to configure PV/PVCs support: {e}')

        # Now that k8s is configured, we would want to scale down any deployment/statefulset which might
        # be consuming a locked host path volume
        await self.middleware.call('chart.release.scale_down_resources_consuming_locked_paths')

        # Let's run app migrations if any
        await self.middleware.call('k8s.app.migration.run')

        node_config = await self.middleware.call('k8s.node.config')
        await self.middleware.call(
            'k8s.node.remove_taints', [
                k['key'] for k in (node_config['spec'].get('taints') or [])
                if k['key'] in ('ix-svc-start', 'ix-svc-stop')
            ]
        )

        # Wait for taints to be removed - this includes the built-in taints which might be present
        # at this point i.e node.kubernetes.io/not-ready
        taint_timeout = 600
        sleep_time = 5
        taints = None
        while taint_timeout > 0:
            taints = (await self.middleware.call('k8s.node.config'))['spec'].get('taints')
            if not taints:
                break

            await asyncio.sleep(sleep_time)
            taint_timeout -= sleep_time
        else:
            raise CallError(
                f'Timed out waiting for {", ".join([taint["key"] for taint in taints])!r} taints to be removed'
            )

        pod_running_timeout = 600
        while pod_running_timeout > 0:
            if await self.middleware.call('k8s.pod.query', [['status.phase', '=', 'Running']]):
                break

            await asyncio.sleep(sleep_time)
            pod_running_timeout -= sleep_time
        else:
            raise CallError('Kube-router routes not applied as timed out waiting for pods to execute')

        # Kube-router configures routes in the main table which we would like to add to kube-router table
        # because it's internal traffic will also be otherwise advertised to the default route specified
        await self.middleware.call('k8s.cni.add_routes_to_kube_router_table')

    @private
    def k8s_props_default(self):
        return {
            'aclmode': 'discard',
            'acltype': 'posix',
            'exec': 'on',
            'setuid': 'on',
            'casesensitivity': 'sensitive',
            'atime': 'off',
        }

    @private
    async def validate_k8s_fs_setup(self):
        config = await self.middleware.call('kubernetes.config')
        if not await self.middleware.call('pool.query', [['name', '=', config['pool']]]):
            raise CallError(f'"{config["pool"]}" pool not found.', errno=errno.ENOENT)

        k8s_datasets = set(await self.kubernetes_datasets(config['dataset']))
        required_datasets = set(config['dataset']) | set(
            os.path.join(config['dataset'], ds) for ds in ('k3s', 'releases')
        )
        existing_datasets = {
            d['id']: d for d in await self.middleware.call(
                'zfs.dataset.query', [['id', 'in', list(k8s_datasets)]], {
                    'extra': {'retrieve_properties': False, 'retrieve_children': False}
                }
            )
        }
        diff = set(existing_datasets) ^ k8s_datasets
        fatal_diff = diff.intersection(required_datasets)
        if fatal_diff:
            raise CallError(f'Missing "{", ".join(fatal_diff)}" dataset(s) required for starting kubernetes.')

        await self.create_update_k8s_datasets(config['dataset'])

        locked_datasets = [
            d['id'] for d in filter(
                lambda d: d['mountpoint'], await self.middleware.call('zfs.dataset.locked_datasets')
            )
            if d['mountpoint'].startswith(f'{config["dataset"]}/') or d['mountpoint'] in (
                f'/mnt/{k}' for k in (config['dataset'], config['pool'])
            )
        ]
        if locked_datasets:
            raise CallError(
                f'Please unlock following dataset(s) before starting kubernetes: {", ".join(locked_datasets)}',
                errno=CallError.EDATASETISLOCKED,
            )

        iface_errors = await self.middleware.call('kubernetes.validate_interfaces', config)
        if iface_errors:
            raise CallError(f'Unable to lookup configured interfaces: {", ".join([v[1] for v in iface_errors])}')

        errors = await self.middleware.call('kubernetes.validate_config')
        if errors:
            raise CallError(str(errors))

        await self.middleware.call('k8s.migration.scale_version_check')

    @private
    def status_change(self):
        config = self.middleware.call_sync('kubernetes.config')
        if self.middleware.call_sync('service.started', 'kubernetes'):
            self.middleware.call_sync('service.stop', 'kubernetes')

        if not config['pool']:
            return

        config_path = os.path.join('/mnt', config['dataset'], 'config.json')
        clean_start = True
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                try:
                    on_disk_config: dict = json.loads(f.read())
                except json.JSONDecodeError:
                    pass
                else:
                    on_disk_config.setdefault('passthrough_mode', False)

                    clean_start = not all(
                        config[k] == on_disk_config.get(k) for k in (
                            'cluster_cidr', 'service_cidr', 'cluster_dns_ip', 'passthrough_mode',
                        )
                    )

        if clean_start and self.middleware.call_sync(
            'zfs.dataset.query', [['id', '=', config['dataset']]], {
                'extra': {'retrieve_children': False, 'retrieve_properties': False}
            }
        ):
            self.middleware.call_sync('zfs.dataset.delete', config['dataset'], {'force': True, 'recursive': True})

        self.middleware.call_sync('kubernetes.setup_pool')
        try:
            self.middleware.call_sync('kubernetes.status_change_internal')
        except Exception as e:
            self.middleware.call_sync('alert.oneshot_create', 'ApplicationsConfigurationFailed', {'error': str(e)})
            self.middleware.call_sync('kubernetes.set_status', Status.FAILED.value, str(e))
            raise
        else:
            with open(config_path, 'w') as f:
                f.write(json.dumps(config))

            self.middleware.call_sync('catalog.sync_all')
            self.middleware.call_sync('alert.oneshot_delete', 'ApplicationsConfigurationFailed', None)

    @private
    async def status_change_internal(self):
        await self.set_status(Status.INITIALIZING.value)
        await self.validate_k8s_fs_setup()
        await self.middleware.call('k8s.migration.run')
        await self.middleware.call('service.start', 'kubernetes')

    @private
    async def setup_pool(self):
        config = await self.middleware.call('kubernetes.config')
        await self.create_update_k8s_datasets(config['dataset'])
        # We will make sure that certificate paths point to the newly configured pool
        await self.middleware.call('kubernetes.update_server_credentials', config['dataset'])
        # Now we would like to setup catalogs
        await self.middleware.call('catalog.sync_all')

    @private
    def get_dataset_update_props(self, props: typing.Dict) -> typing.Dict:
        return {
            attr: value
            for attr, value in props.items()
            if attr not in ('casesensitivity', 'mountpoint', 'encryption')
        }

    @private
    async def create_update_k8s_datasets(self, k8s_ds):
        create_props_default = self.k8s_props_default()
        for dataset_name in await self.kubernetes_datasets(k8s_ds):
            custom_props = self.kubernetes_dataset_custom_props(ds=dataset_name.split('/', 1)[-1])
            # got custom properties, need to re-calculate
            # the update and create props.
            create_props = dict(create_props_default, **custom_props) if custom_props else create_props_default
            update_props = self.get_dataset_update_props(create_props)

            dataset = await self.middleware.call(
                'zfs.dataset.query', [['id', '=', dataset_name]], {
                    'extra': {
                        'properties': list(update_props),
                        'retrieve_children': False,
                        'user_properties': False,
                    }
                }
            )
            if not dataset:
                test_path = os.path.join('/mnt', dataset_name)
                if os.path.exists(test_path):
                    await self.middleware.run_in_thread(
                        shutil.move, test_path, f'{test_path}-{str(uuid.uuid4())[:4]}-{datetime.now().isoformat()}',
                    )
                await self.middleware.call(
                    'zfs.dataset.create', {
                        'name': dataset_name, 'type': 'FILESYSTEM', 'properties': create_props,
                    }
                )
                if create_props.get('mountpoint') != 'legacy':
                    # since, legacy mountpoints should not be zfs mounted.
                    await self.middleware.call('zfs.dataset.mount', dataset_name)
            elif any(val['value'] != update_props[name] for name, val in dataset[0]['properties'].items()):
                await self.middleware.call(
                    'zfs.dataset.update', dataset_name, {
                        'properties': {k: {'value': v} for k, v in update_props.items()}
                    }
                )

    @private
    async def kubernetes_datasets(self, k8s_ds):
        return [k8s_ds] + [
            os.path.join(k8s_ds, d) for d in (
                'k3s', 'k3s/kubelet', 'releases',
                'default_volumes', 'catalogs'
            )
        ]

    @private
    def kubernetes_dataset_custom_props(self, ds: str) -> typing.Dict:
        props = {
            'ix-applications': {
                'encryption': 'off'
            },
            'ix-applications/k3s/kubelet': {
                'mountpoint': 'legacy'
            }
        }
        return props.get(ds, dict())

    @private
    async def start_service(self):
        await self.set_status(Status.INITIALIZING.value)
        try:
            if not await self.middleware.call('kubernetes.license_active'):
                raise CallError('System is not licensed to use Applications')

            await self.before_start_check()
            await self.middleware.call('k8s.migration.scale_version_check')
            await self.middleware.call('k8s.migration.run')
            await self.middleware.call('service.start', 'kubernetes')
        except Exception as e:
            await self.set_status(Status.FAILED.value, str(e))
            raise

    @private
    async def before_start_check(self):
        try:
            await self.middleware.call('kubernetes.validate_k8s_fs_setup')
        except CallError as e:
            if e.errno != CallError.EDATASETISLOCKED:
                await self.middleware.call(
                    'alert.oneshot_create',
                    'ApplicationsConfigurationFailed',
                    {'error': e.errmsg},
                )

            await self.set_status(Status.FAILED.value, f'Could not validate applications setup ({e.errmsg})')
            raise

        await self.middleware.call('alert.oneshot_delete', 'ApplicationsConfigurationFailed', None)

    @private
    async def set_status(self, new_status, extra=None):
        assert new_status in Status.__members__
        new_status = Status(new_status)
        self.STATUS = APPS_STATUS(
            new_status,
            f'{STATUS_DESCRIPTIONS[new_status]}:\n{extra}' if extra else STATUS_DESCRIPTIONS[new_status],
        )
        self.middleware.send_event('kubernetes.state', 'CHANGED', fields=await self.get_status_dict())

    @private
    async def get_status_dict(self):
        return {'status': self.STATUS.status.value, 'description': self.STATUS.description}

    @accepts()
    @returns(Dict(
        Str('status', enum=[e.value for e in Status]),
        Str('description'),
    ))
    async def status(self):
        """
        Returns the status of the Kubernetes service.
        """
        return await self.get_status_dict()

    @private
    async def initialize_k8s_status(self):
        if not await self.middleware.call('system.ready'):
            # Status will be automatically updated when system is ready
            return

        if not (await self.middleware.call('kubernetes.config'))['pool']:
            await self.set_status(Status.UNCONFIGURED.value)
        else:
            if await self.middleware.call('service.started', 'kubernetes'):
                await self.set_status(Status.RUNNING.value)
            else:
                await self.set_status(Status.FAILED.value)


async def _event_system_ready(middleware, event_type, args):
    # we ignore the 'ready' event on an HA system since the failover event plugin
    # is responsible for starting this service
    if await middleware.call('failover.licensed'):
        return

    if (await middleware.call('kubernetes.config'))['pool']:
        middleware.create_task(middleware.call('kubernetes.start_service'))
    else:
        await middleware.call('kubernetes.set_status', Status.UNCONFIGURED.value)


async def _event_system_shutdown(middleware, event_type, args):
    if await middleware.call('service.started', 'kubernetes'):
        middleware.create_task(middleware.call('service.stop', 'kubernetes'))


async def setup(middleware):
    middleware.event_register('kubernetes.state', 'Kubernetes state events')
    middleware.event_subscribe('system.ready', _event_system_ready)
    middleware.event_subscribe('system.shutdown', _event_system_shutdown)
    await middleware.call('kubernetes.initialize_k8s_status')
