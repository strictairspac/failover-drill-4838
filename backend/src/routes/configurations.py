from fastapi import APIRouter, Depends, HTTPException
from src.config.database import get_db
from src.middlewares.auth import current_user, admin
from src.schemas.configuration import ConfigurationInput
from src.services.configuration_service import (
    create_configuration, activate_configuration, cancel_configuration,
    list_configurations, configuration_record, active_configuration,
    configuration_transaction)
from src.controllers.actions import audit
router = APIRouter()


@router.get('/configurations')
def listing(db=Depends(get_db), user=Depends(current_user)):
    return list_configurations(db)


@router.get('/configurations/active')
def current(db=Depends(get_db), user=Depends(current_user)):
    active = active_configuration(db)
    if active is None: raise HTTPException(404, '尚无生效组态')
    return configuration_record(db, active)


@router.post('/configurations', status_code=201)
def create(data: ConfigurationInput, db=Depends(get_db), user=Depends(admin)):
    """Prepare a new voter group.  The old group stays active until activation."""
    with configuration_transaction(db):
        obj, created = create_configuration(db, data, user.email)
        if created:
            audit(db, user, f'configuration:prepare:{obj.id}')
    return configuration_record(db, obj)


@router.post('/configurations/{config_id}/activate')
def activate(config_id: int, db=Depends(get_db), user=Depends(admin)):
    """Activate the prepared group once every member has caught up.

    Idempotent: re-submitting an already activated change reports "activated"
    without creating a new generation.
    """
    with configuration_transaction(db):
        outcome = activate_configuration(db, config_id, user.email)
        obj = outcome['configuration']
        if outcome['activated']:
            action = 'configuration:activate' if outcome['changed'] else 'configuration:activate:noop'
            audit(db, user, f'{action}:{obj.id}')
        else:
            audit(db, user, f'configuration:activate:blocked:{obj.id}')
    record = configuration_record(db, obj)
    if outcome['activated']:
        return {**record, 'activated': True, 'changed': outcome['changed']}
    return {**record, 'activated': False, 'changed': False,
            'blocked_reason': outcome['reason']}


@router.post('/configurations/{config_id}/cancel')
def cancel(config_id: int, db=Depends(get_db), user=Depends(admin)):
    """Abandon a preparing change; the active group is untouched."""
    with configuration_transaction(db):
        obj, changed = cancel_configuration(db, config_id, user.email)
        audit(db, user, f"configuration:cancel:{obj.id}" if changed
              else f'configuration:cancel:noop:{obj.id}')
    return {**configuration_record(db, obj), 'cancelled': True, 'changed': changed}
