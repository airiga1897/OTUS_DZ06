"""Destructive, explicitly selected test cycle, exclusively owning a fresh stand."""
import copy
import json
import logging
import uuid
import urllib.error
from datetime import datetime, timezone

from stack_config import load as stack_settings
from topology import assert_preserved, validate_extension
from cloud_settings import terraform_root

LOGGER = logging.getLogger(__name__)


def require_access(api, cleanup: bool = False) -> None:
    marker = api.LOCAL / 'test-cycle.json'
    if marker.exists():
        value = json.loads(marker.read_text(encoding='utf-8'))
        if value['run_id'] != api._TEST_CYCLE_ID and not (cleanup and value.get('status') == 'cleanup_failed'):
            raise RuntimeError('An unfinished test-cycle owns this clone; preserve its recovery files')


def reject_existing(api) -> None:
    """Read local ownership before authentication and all cloud namespaces before writes."""
    if api.managed_ids():
        raise RuntimeError('test-cycle requires an empty stand')
    for state in api.ROOT.glob('terraform*/terraform.tfstate*'):
        from local_runtime import checked_tree
        checked_tree(state, api.ROOT.resolve())
        data = json.loads(state.read_text())
        if data.get('outputs') or any(r.get('instances') for r in data.get('resources', [])):
            raise RuntimeError('test-cycle refuses a nonempty state or backup')
    pending = ['context.json', 'placement.json', 'configured.json', 'cleanup-fips.json', 'expansion-pending.json', 'test-cycle.json']
    if any((api.LOCAL / name).exists() for name in pending) or (terraform_root(api.ROOT) / 'terraform.tfvars.json').exists():
        raise RuntimeError('test-cycle refuses an existing context or unfinished operation')
    cloud = api.Cloud(bind=False)
    for region in cloud.resource_regions():
        reject_cloud_resources(api, cloud.read_only_pool(region))


def reject_cloud_resources(api, cloud) -> None:
    from cloud_settings import adapter
    adapter().reject_cloud_resources(api, cloud)


def expanded_topology(initial: dict) -> dict:
    if len(initial['nodes']) - 1 >= stack_settings()['postgresql']['max_nodes']:
        raise ValueError('No spare PostgreSQL replication capacity for test-cycle expansion')
    desired = copy.deepcopy(initial)
    hosts = {v['host'] for v in desired['nodes'].values()}
    index = next(i for i in range(1, stack_settings()['postgresql']['max_nodes'] + 2)
                 if 'db' + str(i) not in desired['nodes'])
    host = next(h for h in range(21, 255) if h not in hosts)
    desired['nodes']['db' + str(index)] = {'host': host, 'role': 'db'}
    validate_extension(initial, desired)
    return desired


def check_original(api, snapshot: dict) -> None:
    from verification import expansion
    expansion(api, snapshot)


def progress(api, stage: str) -> None:
    report = getattr(api, '_CYCLE_PROGRESS', None)
    if isinstance(report, dict):
        if report['stage'] != 'starting':
            report['completed_steps'].append(report['stage'])
        report['stage'] = stage
        report['steps'].append(stage)
        api.save(api.ROOT / 'docs/test-cycle-result.json', report)


def stages(api) -> dict:
    from verification import snapshot
    initial = api.desired_topology()
    progress(api, 'initial_deploy')
    api.deploy_selected()
    before = api.managed_ids()
    progress(api, 'control_data_and_media')
    canary = snapshot(api, api.lab_output())
    progress(api, 'repeat_deploy')
    api.deploy()
    assert_preserved(before, api.managed_ids())
    check_original(api, canary)
    progress(api, 'failover_and_recovery')
    api.verify(True)
    progress(api, 'stages_completed')
    return {'success': True, 'existing_ids_preserved': True, 'repeat_deploy_verified': True,
            'database_nodes': len(initial['nodes']) - 1,
            'failover_and_recovery_verified': True,
            'original_data_and_media_preserved': True}


def run(api) -> None:
    reject_existing(api)
    # Snapshot configuration in memory: the repository topology is never rewritten.
    api.LOCAL.mkdir(parents=True, exist_ok=True)
    marker = api.LOCAL / 'test-cycle.json'
    run_id = uuid.uuid4().hex
    with marker.open('x', encoding='utf-8') as stream:
        json.dump({'run_id': run_id, 'status': 'running'}, stream)
    api._TEST_CYCLE_ID = run_id
    original = api._TOPOLOGY_OVERRIDE
    (api.ROOT / 'docs').mkdir(parents=True, exist_ok=True)
    result = {'run_id': run_id, 'started_utc': datetime.now(timezone.utc).isoformat(),
              'success': False, 'stages_completed': False,
              'state_and_api_cleanup_verified': False, 'steps': [],
              'completed_steps': [], 'stage': 'starting'}
    api._CYCLE_PROGRESS = result
    try:
        api.save(api.ROOT / 'docs/test-cycle-result.json', result)
        result.update(stages(api))
        result['stages_completed'] = True
    except BaseException as error:
        result['success'] = False
        result['error_type'] = type(error).__name__
        result['failed_stage'] = result['stage']
        raise
    finally:
        api._TOPOLOGY_OVERRIDE = original
        try:
            require_access(api)
            api.cleanup()
            result['state_and_api_cleanup_verified'] = True
        except BaseException as error:
            result['success'] = False
            result['cleanup_error_type'] = type(error).__name__
            api.save(marker, {'run_id': run_id, 'status': 'cleanup_failed'})
            LOGGER.error('Test cleanup failed: preserve state, context and keys; retry destroy')
            raise
        finally:
            api._TEST_CYCLE_ID = None
            api._CYCLE_PROGRESS = None
            result['finished_utc'] = datetime.now(timezone.utc).isoformat()
            api.save(api.ROOT / 'docs/test-cycle-result.json', result)
