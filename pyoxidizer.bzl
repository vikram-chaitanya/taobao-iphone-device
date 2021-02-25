def make_dist():
    return default_python_distribution()

def make_exe(dist):
    policy = dist.make_python_packaging_policy()
    policy.resources_location_fallback = "filesystem-relative:lib"

    python_config = dist.make_python_interpreter_config()

    python_config.oxidized_importer = True
    python_config.filesystem_importer = True

    #python_config.run_command = "from tidevice.__main__ import main; main()"
    python_config.run_module = "tidevice"

    exe = dist.to_python_executable(
        name="tidevice",
        packaging_policy=policy,
        config=python_config,
    )

    exe.add_python_resources(exe.read_package_root(
        path=".",
        packages=["tidevice"],
    ))

    for resource in exe.pip_install(["."]):
        resource.add_location = "filesystem-relative:lib"
        exe.add_python_resource(resource)

    return exe

def make_embedded_resources(exe):
    return exe.to_embedded_resources()

def make_install(exe):
    files = FileManifest()
    files.add_python_resource("./dist", exe)
    return files

register_target("dist", make_dist)
register_target("exe", make_exe, depends=["dist"])
register_target("resources", make_embedded_resources, depends=["exe"], default_build_script=True)
register_target("install", make_install, depends=["exe"], default=True)

resolve_targets()

PYOXIDIZER_VERSION = "0.10.3"
PYOXIDIZER_COMMIT = "UNKNOWN"
