# -*- coding: utf-8 -*- {{{
# ===----------------------------------------------------------------------===
#
#                 Component of Eclipse VOLTTRON
#
# ===----------------------------------------------------------------------===
#
# Copyright 2026 Battelle Memorial Institute
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy
# of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations
# under the License.
#
# ===----------------------------------------------------------------------===
# }}}
import os
import sys

from importlib.metadata import requires
from setuptools import setup, find_packages
from subprocess import run

try:
    import tomli
except ModuleNotFoundError:
    run(["pip", "install", "tomli"], check=True)
    import tomli

# Install Base Driver library without dependencies to avoid pulling in volttron-core.
run(['pip', 'install', '--no-deps', 'volttron-lib-base-driver>=2.0.0rc2'], check=True)

# Discover dependencies from metadata in the newly installed volttron-lib-base-driver package.
exclude_packages = ['python', 'volttron-core', 'volttron-lib-base-driver']
base_deps = [f'{d}{v.strip("()")}' for d, v in [x.split(' ') for x in requires('volttron-lib-base-driver')]
             if d not in exclude_packages]

# Discover dependencies of Platform Driver Agent from pyproject.toml.
with open('pyproject.toml', 'rb') as f:
    ppt = tomli.load(f)
agent_deps = [f'{d}{v}' for d, v in ppt['tool']['poetry'].get('dependencies', {}).items() if d not in exclude_packages]

# Install all dependencies.
deps = base_deps + agent_deps
run(['pip', 'install', *deps], check=True)

MAIN_MODULE = 'agent'

# Find the agent package that contains the main module
packages = find_packages("./src")
agent_package = ""
for package in packages:
    # Because there could be other packages such as tests
    if os.path.isfile(f'src/{package}/{MAIN_MODULE}.py'):
        agent_package = package
        break

if not agent_package:
    raise RuntimeError(
        f"None of the packages under {os.path.abspath('.')} contain the file {MAIN_MODULE}.py"
    )

# Find the version number from the main module
agent_module = f"{agent_package}.{MAIN_MODULE}"
sys.path.append('src')
_temp = __import__(f'{agent_module}', globals(), locals(), ["__version__"], 0)
__version__ = _temp.__version__

setup(
    name=f"{agent_package}agent",
    version=__version__,
    packages=packages,
    package_dir={'': 'src'},
    entry_points={
        "setuptools.installation": [f"eggsecutable = {agent_module}:main"]
    },
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "Topic :: Home Automation",
        "Topic :: Software Development :: Embedded Systems",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
)