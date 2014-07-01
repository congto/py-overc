import os
import logging
from time import sleep

from overc.lib.db import models
from overc.lib import alerts

logger = logging.getLogger(__name__)

# TODO: these routines should obtain an exclusive lock so multiple supervise processes does not issue alerts multiple times


def _check_service_states(ssn):
    """ Test all service states, raise alerts if necessary
    :param ssn: Database session
    :type ssn: sqlalchemy.orm.session.Session
    :returns: The number of new alerts reported
    :rtype: int
    """
    # Fetch all states that are not yet checked
    service_states = ssn.query(models.ServiceState)\
        .filter(models.ServiceState.checked == False)\
        .order_by(models.ServiceState.id.asc())\
        .all()

    # Check them one by one
    new_alerts = 0
    for s in service_states:
        alert = None
        logger.debug(u'Checking service {server}:`{service}` state #{id}: {state}'.format(id=s.id, server=s.service.server, service=s.service, state=s.state))

        # Report state changes and abnormal states
        if s.state != (s.prev.state if s.prev else 'OK'):
            ssn.add(models.Alert(
                server=s.service.server,
                service=s.service,
                channel='service:state',
                event='changed',
                message=u'State changed: "{}" -> "{}"'.format(s.prev.state if s.prev else '(?)', s.state)
            ))
            new_alerts += 1

            # In addition, report "UNK" states!
            if s.state == 'UNK' and s.prev is not None:
                ssn.add(models.Alert(
                    server=s.service.server,
                    service=s.service,
                    channel='service:state',
                    event='unk',
                    message=u'Service state unknown!'
                ))
                new_alerts += 1

        # Save
        s.checked = True
        ssn.add(s)

    # Finish
    ssn.commit()
    return new_alerts


def _check_service_timeouts(ssn):
    """ Test all services for timeouts
    :param ssn: Database session
    :type ssn: sqlalchemy.orm.session.Session
    :returns: The number of new alerts reported
    :rtype: int
    """
    # Fetch all services which have enough data
    services = ssn.query(models.Service)\
        .filter(
            models.Service.period is not None,
            models.Service.state is not None
        ).all()

    # Detect timeouts
    new_alerts = 0
    for s in services:
        # Update state
        was_timed_out = s.timed_out
        seen_ago = s.update_timed_out()

        logger.debug(u'Checking service {service}: timed_out={service.timed_out}, seen_ago={seen_ago}'.format(service=s, seen_ago=seen_ago))

        # Changed?
        if was_timed_out != s.timed_out:
            alert = models.Alert(
                server=s.server,
                service=s,
                channel='service'
            )
            if s.timed_out:
                alert.event = 'offline'
                alert.message = u'Service offline: last seen {} ago'.format(
                    str(seen_ago).split('.')[0]
                )
            else:
                alert.event = 'online'
                alert.message = u'Service back online'

            ssn.add(alert)
            ssn.add(s)
            new_alerts += 1

    # Finish
    ssn.commit()
    return new_alerts


def _send_pending_alerts(ssn, alertd_path, alerts_config):
    """ Send pending alerts
    :param ssn: Database session
    :type ssn: sqlalchemy.orm.session.Session
    :param alertd_path: Path to "alert.d" instance folder
    :type alertd_path: str
    :param alerts_config: Application config for alerts
    :type alerts_config: dict
    :returns: The number of alerts sent
    :rtype: int
    """
    # Fetch all alerts which were not reported
    pending_alerts = ssn.query(models.Alert)\
        .filter(models.Alert.reported == False)\
        .all()

    # Report them one by one
    for a in pending_alerts:
        logger.debug(u'Sending alert #{id}: server={server}, service={service}, [{channel}/{event}]'.format(id=a.id, server=a.server, service=a.service, channel=a.channel, event=a.event))

        # Prepare alert message
        alert_message = unicode(a) + "\n"
        if a.service and a.service.state:
            s = a.service.state
            alert_message += u"Current: {}: {}\n".format(s.state, s.info)

        # Potential exceptions are handled & logged down there
        alerts.send_alert_to_subscribers(alertd_path, alerts_config, alert_message)
        a.reported = True
        ssn.add(a)

    # Finish
    ssn.commit()
    return len(pending_alerts)


def supervise_once(app):
    """ Perform all background actions once:

    * Check service states
    * Check for service timeouts
    * Send alerts

    :param app: Application
    :type app: OvercApplication
    :returns: (New alerts created, Alerts sent)
    :rtype: (int, int)
    """

    # Prepare
    ssn = app.db
    alertd_path = os.path.join(app.app.instance_path, 'alert.d')
    alerts_config = app.app.config['ALERTS']

    # Act
    new_alerts, sent_alerts = 0, 0
    new_alerts += _check_service_states(ssn)
    new_alerts += _check_service_timeouts(ssn)
    sent_alerts = _send_pending_alerts(ssn, alertd_path, alerts_config)

    # Finish
    return new_alerts, sent_alerts


def supervise_loop(app):
    """ Supervisor main loop which performs background actions
    :param app: Application
    :type app: OvercApplication
    """
    while True:
        try:
            supervise_once(app)
            sleep(5)
        except Exception as e:
            logger.exception('Supervise loop error')
            # proceed: this loop is important and should never halt
