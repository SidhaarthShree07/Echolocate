from setuptools import setup, find_packages

setup(
    name="echolocate",
    version="1.0.0",
    description="Offline Voice Accessibility Agent",
    author="EchoLocate Team",
    packages=find_packages(include=["echolocate", "echolocate.*"]),
    install_requires=[
        "google-adk==2.3.0",
        "litellm==1.82.6",
        "silero-vad-lite==0.2.1",
        "kokoro-onnx>=0.5.0",
        "faster-whisper==1.1.1",
        "sounddevice",
        "PyYAML",
    ],
    entry_points={
        "console_scripts": [
            "echolocate=echolocate.cli:main",
        ],
    },
)
