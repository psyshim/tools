"""The coding service: owns the task loop, the durable task/event state and the model; never touches a workspace itself.

It reaches files, Git and test commands only through a Device Agent over a typed channel (see device_client.py). Nothing in
this package may import `coding_agent`, `code_attach.tools` or any tool/file/Git module (tests/test_coding_service_isolation.py).
"""
