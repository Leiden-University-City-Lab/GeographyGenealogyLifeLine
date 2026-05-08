from setuptools import setup, find_packages

setup(
    name='person_card',
    version='0.1.0',
    author='oscar',
    description='Custom Dash component — PersonCard with mini SVG timeline',
    packages=find_packages(),
    include_package_data=True,
    package_data={
        'person_card': ['person_card/*.js', 'person_card/*.map'],
    },
    install_requires=['dash'],
)
