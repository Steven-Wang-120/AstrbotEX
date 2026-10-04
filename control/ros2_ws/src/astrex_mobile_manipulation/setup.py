from setuptools import find_packages, setup
from glob import glob
setup(name='astrex_mobile_manipulation', version='0.1.0', packages=find_packages(),
 data_files=[('share/ament_index/resource_index/packages',['resource/astrex_mobile_manipulation']),
 ('share/astrex_mobile_manipulation',['package.xml']),
 ('share/astrex_mobile_manipulation/launch',glob('launch/*.launch.py')),
 ('share/astrex_mobile_manipulation/config',glob('config/*'))],
 install_requires=['setuptools'], zip_safe=True, maintainer='AstrEX', maintainer_email='maintainer@example.invalid',
 description='Simulation-only bounded ROS gateway and feedback gate', license='Apache-2.0',
 entry_points={'console_scripts':['mm_gateway = astrex_mobile_manipulation.gateway:main']})
