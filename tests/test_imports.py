import subprocess
import sys


def test_importing_fixie_loads_no_azure_modules():
    code = (
        "import sys, fixie.settings, fixie.model, fixie.gateway, fixie.fake_gateway, fixie.status_comment, fixie.allowlist; "
        "sys.exit(any(m.split('.')[0] in ('azure', 'msrest', 'anthropic') for m in sys.modules))"
    )
    result = subprocess.run([sys.executable, "-c", code])
    assert result.returncode == 0
