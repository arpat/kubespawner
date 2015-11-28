from jupyterhub.spawner import Spawner
from tornado import gen
from requests_futures.sessions import FuturesSession
import json
import time
from traitlets import Unicode


class UnicodeOrFalse(Unicode):
    info_text = 'a unicode string or False'

    def validate(self, obj, value):
        if value is False:
            return value
        return super(UnicodeOrFalse, self).validate(obj, value)


class KubeSpawner(Spawner):
    kube_api_endpoint = Unicode(
        config=True,
        help='Endpoint to use for kubernetes API calls'
    )

    kube_api_version = Unicode(
        'v1',
        config=True,
        help='Kubernetes API version to use'
    )

    kube_namespace = Unicode(
        'jupyter',
        config=True,
        help='Kubernetes Namespace to create pods in'
    )

    pod_name_template = Unicode(
        'jupyter-{user}',
        config=True,
        help='Template to generate pod names. Supports: {user} for username'
    )

    hub_ip_connect = Unicode(
        "",
        config=True,
        help='Endpoint that containers should use to contact the hub'
    )

    kube_ca_path = UnicodeOrFalse(
        '/var/run/secrets/kubernetes.io/serviceaccount/ca.crt',
        config=True,
        help='Path to the CA crt to use to connect to the kube API server'
    )

    kube_token = Unicode(
        '',
        config=True,
        help='Kubernetes API authorization token'
    )

    singleuser_image_spec = Unicode(
        'jupyter/singleuser',
        config=True,
        help='Name of Docker image to use when spawning user pods'
    )

    cpu_limit = Unicode(
        "2000m",
        config=True,
        help='Max number of CPU cores that a single user can use'
    )

    cpu_request = Unicode(
        "200m",
        config=True,
        help='Min nmber of CPU cores that a single user is guaranteed'
    )

    mem_limit = Unicode(
        "1Gi",
        config=True,
        help='Max amount of memory a single user can use'
    )

    mem_request = Unicode(
        "128Mi",
        config=True,
        help='Min amount of memory a single user is guaranteed'
    )

    def get_pod_manifest(self):
        return {
            'apiVersion': 'v1',
            'kind': 'Pod',
            'metadata': {
                'name': self.pod_name,
                'labels': {
                    'name': self.pod_name
                }
            },
            'spec': {
                'containers': [
                    {
                        'name': 'jupyter',
                        'image': self.singleuser_image_spec,
                        'resources': {
                            'requests': {
                                'memory': self.mem_request,
                                'cpu': self.cpu_request,
                            },
                            'limits': {
                                'memory': self.mem_limit,
                                'cpu': self.cpu_limit
                            }
                        },
                        'env': [
                            {'name': k, 'value': v}
                            for k, v in self._env_default().items()
                        ]
                    }
                ]
            }
        }

    def _get_pod_url(self, pod_name=None):
        url = '{host}/api/{version}/namespaces/{namespace}/pods'.format(
            host=self.kube_api_endpoint,
            version=self.kube_api_version,
            namespace=self.kube_namespace
        )
        if pod_name:
            return url + '/' + pod_name
        return url

    @property
    def session(self):
        if hasattr(self, '_session'):
            return self._session
        else:
            self._session = FuturesSession()
            auth_header = 'Bearer %s' % self.kube_token
            self._session.headers['Authorization'] = auth_header
            self._session.verify = self.kube_ca_path
            return self._session

    def load_state(self, state):
        super(KubeSpawner, self).load_state(state)
        self.log.info(repr(state))

    def get_state(self):
        state = super(KubeSpawner, self).get_state()
        state['hi'] = 'hello'
        return state

    @gen.coroutine
    def get_pod_info(self, pod_name):
        resp = self.session.get(
            self._get_pod_url(),
            params={'labelSelector': 'name = %s' % pod_name})
        data = yield resp
        return data.json()

    def is_pod_running(self, pod_info):
        return 'items' in pod_info and len(pod_info['items']) > 0 and \
            pod_info['items'][0]['status']['phase'] == 'Running' and \
            pod_info['items'][0]['status']['conditions'][0]['type'] == 'Ready'

    @property
    def pod_name(self):
        return self.pod_name_template.format(
            user=self.user.id
        )

    @gen.coroutine
    def poll(self):
        data = yield self.get_pod_info(self.pod_name)
        self.log.info(repr(data))
        if self.is_pod_running(data):
            return None
        return 1

    @gen.coroutine
    def start(self):
        self.log.info('start called')
        pod_manifest = self.get_pod_manifest()
        self.log.info(self._get_pod_url())
        resp = yield self.session.post(
            self._get_pod_url(),
            data=json.dumps(pod_manifest))
        self.log.info(repr(resp.headers))
        self.log.info(repr(resp.text))
        while True:
            data = yield self.get_pod_info(self.pod_name)
            self.log.info(data)
            if self.is_pod_running(data):
                break
            self.log.info('not ready yet!')
            time.sleep(5)
        self.user.server.ip = data['items'][0]['status']['podIP']
        self.user.server.port = 8888
        self.db.commit()
        self.log.info(pod_manifest)

    @gen.coroutine
    def stop(self):
        self.log.info('stop called! boo!')
        resp = yield self.session.delete(self._get_pod_url(self.pod_name))
        self.log.info(resp.text)

    def _public_hub_api_url(self):
        if self.hub_ip_connect:
            proto, path = self.hub.api_url.split('://', 1)
            ip, rest = path.split('/', 1)
            return '{proto}://{ip}/{rest}'.format(
                    proto=proto,
                    ip=self.hub_ip_connect,
                    rest=rest
                )
        else:
            return self.hub.api_url

    def _env_keep_default(self):
        return []

    def _env_default(self):
        env = super(KubeSpawner, self)._env_default()
        env.update(dict(
                    JPY_USER=self.user.name,
                    JPY_COOKIE_NAME=self.user.server.cookie_name,
                    JPY_BASE_URL=self.user.server.base_url,
                    JPY_HUB_PREFIX=self.hub.server.base_url,
                    JPY_HUB_API_URL=self._public_hub_api_url()
                ))
        return env
