import asyncio
import os.path

from dateutil.parser import parse as datetime_parse

from .client import K8sClientBase
from .exceptions import ApiException
from .utils import NODE_NAME, RequestMode


class CoreAPI(K8sClientBase):

    NAMESPACE = '/api/v1/namespaces'


class Namespace(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/namespaces'


class Node(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/nodes'
    OBJECT_HUMAN_NAME = 'Node'

    @classmethod
    async def get_instance(cls, **kwargs) -> dict:
        return await super().get_instance(NODE_NAME, **kwargs)

    @classmethod
    async def add_taint(cls, taint_dict: dict) -> None:
        for k in ('key', 'effect'):
            assert k in taint_dict

        node_object = await cls.get_instance()
        existing_taints = []
        for taint in (node_object['spec']['taints'] if node_object['spec'].get('taints') else []):
            if all(taint.get(k) == taint_dict.get(k) for k in ('key', 'effect', 'value')):
                return
            existing_taints.append(taint)

        await cls.update(
            node_object['metadata']['name'], {'spec': {'taints': existing_taints + [taint_dict]}}
        )

    @classmethod
    async def remove_taint(cls, taint_key: str) -> None:
        node_object = await cls.get_instance()
        taints = node_object['spec'].get('taints') or []

        indexes = []
        for index, taint in enumerate(taints):
            if taint['key'] == taint_key:
                indexes.append(index)

        if not indexes:
            raise ApiException(f'Unable to find taint with "{taint_key}" key')

        for index in sorted(indexes, reverse=True):
            taints.pop(index)

        await cls.update(node_object['metadata']['name'], {'spec': {'taints': taints}})

    @classmethod
    async def get_stats(cls):
        return await cls.call(
            os.path.join(cls.uri(object_name=NODE_NAME), 'proxy/stats/summary'), mode=RequestMode.GET.value
        )


class Service(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/services'
    OBJECT_HUMAN_NAME = 'Service'
    OBJECT_TYPE = 'services'


class Pod(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/pods'
    OBJECT_HUMAN_NAME = 'Pod'
    OBJECT_TYPE = 'pods'
    STREAM_RESPONSE_TIMEOUT = 30
    STREAM_RESPONSE_TYPE = 'text'

    @classmethod
    async def logs(cls, pod_name: str, namespace: str, **kwargs) -> str:
        return await cls.call(
            cls.uri(namespace, pod_name + '/log', parameters=kwargs), mode=RequestMode.GET.value, response_type='text'
        )

    @classmethod
    async def stream_uri(cls, **kwargs) -> str:
        return cls.uri(kwargs.pop('namespace'), kwargs.pop('pod_name') + '/log', parameters={
            'follow': 'true',
            'timestamps': 'true',
            'timeoutSeconds': cls.STREAM_RESPONSE_TIMEOUT * 60,
            **kwargs,
        })

    @classmethod
    def normalize_data(cls, data: str) -> str:
        return data


class Event(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/events'
    OBJECT_HUMAN_NAME = 'Event'
    OBJECT_TYPE = 'events'
    STREAM_RESPONSE_TIMEOUT = 5
    STREAM_RESPONSE_TYPE = 'json'

    @classmethod
    async def query(cls, *args, **kwargs) -> list:
        events = await super().query(*args, **kwargs)
        for event in events['items']:
            cls.sanitize_data_internal(event)
        return events

    @classmethod
    def normalize_data(cls, sanitized: dict) -> dict:
        sanitized['object'] = cls.sanitize_data_internal(sanitized['object'])
        return sanitized

    @classmethod
    def sanitize_data_internal(cls, sanitized: dict) -> dict:
        if sanitized['metadata'].get('creationTimestamp'):
            # TODO: Let's remove this in next major release as this is required right now for backwards
            #  compatibility with existing consumers i.e UI
            sanitized['metadata']['creation_timestamp'] = datetime_parse(sanitized['metadata']['creationTimestamp'])

        return sanitized

    @classmethod
    async def stream_uri(cls, **kwargs) -> str:
        return cls.uri(
            namespace=kwargs.pop('namespace', None), parameters={**kwargs, 'watch': 'true', 'timestamp': 'true'}
        )


class Secret(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/secrets'
    OBJECT_HUMAN_NAME = 'Secret'
    OBJECT_TYPE = 'secrets'


class PersistentVolume(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/persistentvolumes'
    OBJECT_HUMAN_NAME = 'Persistent Volume'
    OBJECT_TYPE = 'persistentvolumes'


class RuntimeClass(CoreAPI):

    OBJECT_ENDPOINT = '/apis/node.k8s.io/v1/runtimeclasses'
    OBJECT_HUMAN_NAME = 'Runtime Class'


class Configmap(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/configmaps'
    OBJECT_HUMAN_NAME = 'Configmap'
    OBJECT_TYPE = 'configmaps'


class ServiceAccount(CoreAPI):

    OBJECT_ENDPOINT = '/api/v1/serviceaccounts'
    OBJECT_HUMAN_NAME = 'Service Account'
    OBJECT_TYPE = 'serviceaccounts'

    @classmethod
    async def create_token(cls, name: str, data: dict, **kwargs) -> str:
        return (await cls.call(cls.uri(
            object_name=name + '/token', namespace=kwargs.pop('namespace', None), parameters=kwargs,
        ), body=data, mode=RequestMode.POST.value))['status']['token']

    @classmethod
    async def safely_create_token(cls, service_account_name: str) -> str:
        while True:
            try:
                service_account_details = await cls.get_instance(service_account_name)
            except Exception:
                await asyncio.sleep(5)
            else:
                break

        return await cls.create_token(
            service_account_name, {'spec': {'expirationSeconds': 500000000}},
            namespace=service_account_details['metadata']['namespace'],
        )
