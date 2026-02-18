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
import logging
import os
import sys

from setuptools import setup, find_packages
from subprocess import check_call, CalledProcessError

_log = logging.getLogger(__name__)


try:
    check_call(['git', 'clone',
                'https://github.com/eclipse-volttron/volttron-lib-base-driver',
                'volttron-lib-base-driver'])
    os.chdir('volttron-lib-base-driver')
    check_call(['python', 'setup.py', '--no-user-cfg', 'bdist_wheel'])
    check_call(['pip', 'install', 'dist/' + os.listdir('dist')[0]])
    os.chdir('..')
except (FileNotFoundError, IndexError, CalledProcessError) as e:
    _log.error(f'Unable to install VOLTTRON Base Driver: {str(e)}.'
               f' Platform Driver Agent will not work until this library has been installed.')

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
    entry_points={"setuptools.installation": [f"eggsecutable = {agent_module}:main"]},
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
