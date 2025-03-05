# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

# NOTE! THIS FILE IS AUTOMATICALLY GENERATED AND WILL BE OVERWRITTEN!
#
# IF YOU WANT TO MODIFY THIS FILE, YOU SHOULD MODIFY THE TEMPLATE
# `get_provider_info_TEMPLATE.py.jinja2` IN the `dev/breeze/src/airflow_breeze/templates` DIRECTORY


def get_provider_info():
    return {
        "package-name": "apache-airflow-providers-yandex",
        "name": "Yandex",
        "description": "This package is for Yandex, including:\n\n    - `Yandex.Cloud <https://cloud.yandex.com/>`__\n",
        "state": "ready",
        "source-date-epoch": 1741121964,
        "versions": [
            "4.0.1",
            "4.0.0",
            "3.12.0",
            "3.11.2",
            "3.11.1",
            "3.11.0",
            "3.10.0",
            "3.9.0",
            "3.8.0",
            "3.7.1",
            "3.7.0",
            "3.6.0",
            "3.5.0",
            "3.4.0",
            "3.3.0",
            "3.2.0",
            "3.1.0",
            "3.0.0",
            "2.2.3",
            "2.2.2",
            "2.2.1",
            "2.2.0",
            "2.1.0",
            "2.0.0",
            "1.0.1",
            "1.0.0",
        ],
        "integrations": [
            {
                "integration-name": "Yandex.Cloud",
                "external-doc-url": "https://cloud.yandex.com/",
                "logo": "/docs/integration-logos/Yandex-Cloud.png",
                "tags": ["service"],
            },
            {
                "integration-name": "Yandex.Cloud Dataproc",
                "external-doc-url": "https://cloud.yandex.com/dataproc",
                "how-to-guide": ["/docs/apache-airflow-providers-yandex/operators/dataproc.rst"],
                "logo": "/docs/integration-logos/Yandex-Cloud.png",
                "tags": ["service"],
            },
            {
                "integration-name": "Yandex.Cloud YQ",
                "external-doc-url": "https://cloud.yandex.com/en/services/query",
                "how-to-guide": ["/docs/apache-airflow-providers-yandex/operators/yq.rst"],
                "logo": "/docs/integration-logos/Yandex-Cloud.png",
                "tags": ["service"],
            },
        ],
        "operators": [
            {
                "integration-name": "Yandex.Cloud Dataproc",
                "python-modules": ["airflow.providers.yandex.operators.dataproc"],
            },
            {
                "integration-name": "Yandex.Cloud YQ",
                "python-modules": ["airflow.providers.yandex.operators.yq"],
            },
        ],
        "hooks": [
            {"integration-name": "Yandex.Cloud", "python-modules": ["airflow.providers.yandex.hooks.yandex"]},
            {
                "integration-name": "Yandex.Cloud Dataproc",
                "python-modules": ["airflow.providers.yandex.hooks.dataproc"],
            },
            {"integration-name": "Yandex.Cloud YQ", "python-modules": ["airflow.providers.yandex.hooks.yq"]},
        ],
        "connection-types": [
            {
                "hook-class-name": "airflow.providers.yandex.hooks.yandex.YandexCloudBaseHook",
                "connection-type": "yandexcloud",
            }
        ],
        "secrets-backends": ["airflow.providers.yandex.secrets.lockbox.LockboxSecretBackend"],
        "extra-links": ["airflow.providers.yandex.links.yq.YQLink"],
        "config": {
            "yandex": {
                "description": "This section contains settings for Yandex Cloud integration.",
                "options": {
                    "sdk_user_agent_prefix": {
                        "description": "Prefix for User-Agent header in Yandex.Cloud SDK requests\n",
                        "version_added": "3.6.0",
                        "type": "string",
                        "example": None,
                        "default": "",
                    }
                },
            }
        },
        "dependencies": ["apache-airflow>=2.9.0", "yandexcloud>=0.308.0", "yandex-query-client>=0.1.4"],
        "devel-dependencies": [],
    }
