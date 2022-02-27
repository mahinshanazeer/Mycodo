# coding=utf-8
import logging
import os
import threading
import time

from mycodo.config import PATH_FUNCTION_ACTIONS
from mycodo.config import PATH_FUNCTION_ACTIONS_CUSTOM
from mycodo.config import SQL_DATABASE_MYCODO
from mycodo.databases.models import Actions
from mycodo.databases.models import Camera
from mycodo.databases.models import Conditional
from mycodo.databases.models import ConditionalConditions
from mycodo.databases.models import Conversion
from mycodo.databases.models import CustomController
from mycodo.databases.models import DeviceMeasurements
from mycodo.databases.models import Input
from mycodo.databases.models import LCD
from mycodo.databases.models import Math
from mycodo.databases.models import NoteTags
from mycodo.databases.models import Notes
from mycodo.databases.models import OutputChannel
from mycodo.databases.models import PID
from mycodo.databases.models import SMTP
from mycodo.databases.models import Trigger
from mycodo.databases.utils import session_scope
from mycodo.devices.camera import camera_record
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.database import db_retrieve_table_daemon
from mycodo.utils.influx import get_last_measurement
from mycodo.utils.influx import get_past_measurements
from mycodo.utils.modules import load_module_from_file
from mycodo.utils.send_data import send_email
from mycodo.utils.system_pi import return_measurement_info

MYCODO_DB_PATH = 'sqlite:///' + SQL_DATABASE_MYCODO

logger = logging.getLogger("mycodo.function_actions")


def parse_function_action_information(exclude_custom=False):
    """Parses the variables assigned in each Function Action and return a dictionary of IDs and values"""
    def dict_has_value(dict_inp, action, key, force_type=None):
        if (key in action.FUNCTION_ACTION_INFORMATION and
                (action.FUNCTION_ACTION_INFORMATION[key] or
                 action.FUNCTION_ACTION_INFORMATION[key] == 0)):
            if force_type == 'list':
                if isinstance(action.FUNCTION_ACTION_INFORMATION[key], list):
                    dict_inp[action.FUNCTION_ACTION_INFORMATION['name_unique']][key] = \
                        action.FUNCTION_ACTION_INFORMATION[key]
                else:
                    dict_inp[action.FUNCTION_ACTION_INFORMATION['name_unique']][key] = \
                        [action.FUNCTION_ACTION_INFORMATION[key]]
            else:
                dict_inp[action.FUNCTION_ACTION_INFORMATION['name_unique']][key] = \
                    action.FUNCTION_ACTION_INFORMATION[key]
        return dict_inp

    excluded_files = [
        '__init__.py', '__pycache__', 'base_function_action.py',
        'custom_function_actions', 'examples', 'scripts', 'tmp_function_actions'
    ]

    function_paths = [PATH_FUNCTION_ACTIONS]

    if not exclude_custom:
        function_paths.append(PATH_FUNCTION_ACTIONS_CUSTOM)

    dict_actions = {}

    for each_path in function_paths:

        real_path = os.path.realpath(each_path)

        for each_file in os.listdir(real_path):
            if each_file in excluded_files:
                continue

            full_path = "{}/{}".format(real_path, each_file)
            function_action = load_module_from_file(full_path, 'actions')

            if not function_action or not hasattr(function_action, 'FUNCTION_ACTION_INFORMATION'):
                continue

            # Populate dictionary of function information
            if function_action.FUNCTION_ACTION_INFORMATION['name_unique'] in dict_actions:
                logger.error(
                    "Error: Cannot add controller modules because it does not have a unique name: {name}".format(
                        name=function_action.FUNCTION_ACTION_INFORMATION['name_unique']))
            else:
                dict_actions[function_action.FUNCTION_ACTION_INFORMATION['name_unique']] = {}

            dict_actions[function_action.FUNCTION_ACTION_INFORMATION['name_unique']]['file_path'] = full_path

            dict_actions = dict_has_value(dict_actions, function_action, 'name')
            dict_actions = dict_has_value(dict_actions, function_action, 'manufacturer')
            dict_actions = dict_has_value(dict_actions, function_action, 'message')
            dict_actions = dict_has_value(dict_actions, function_action, 'usage')
            dict_actions = dict_has_value(dict_actions, function_action, 'url_datasheet', force_type='list')
            dict_actions = dict_has_value(dict_actions, function_action, 'url_manufacturer', force_type='list')
            dict_actions = dict_has_value(dict_actions, function_action, 'url_product_purchase', force_type='list')
            dict_actions = dict_has_value(dict_actions, function_action, 'url_additional', force_type='list')
            dict_actions = dict_has_value(dict_actions, function_action, 'dependencies_module')
            dict_actions = dict_has_value(dict_actions, function_action, 'dependencies_message')
            dict_actions = dict_has_value(dict_actions, function_action, 'custom_options')

    return dict_actions


def check_allowed_to_email():
    smtp_table = db_retrieve_table_daemon(SMTP, entry='first')
    smtp_max_count = smtp_table.hourly_max
    smtp_wait_timer = smtp_table.smtp_wait_timer
    email_count = smtp_table.email_count

    if (email_count >= smtp_max_count and
            time.time() < smtp_wait_timer):
        allowed_to_send_notice = False
    else:
        if time.time() > smtp_wait_timer:
            with session_scope(MYCODO_DB_PATH) as new_session:
                mod_smtp = new_session.query(SMTP).first()
                mod_smtp.email_count = 0
                mod_smtp.smtp_wait_timer = time.time() + 3600
                new_session.commit()
        allowed_to_send_notice = True

    with session_scope(MYCODO_DB_PATH) as new_session:
        mod_smtp = new_session.query(SMTP).first()
        mod_smtp.email_count += 1
        new_session.commit()

    return smtp_wait_timer, allowed_to_send_notice


def get_condition_value(condition_id):
    """
    Returns condition measurements for Conditional controllers
    :param condition_id: Conditional condition ID
    :return: measurement: multiple types
    """
    sql_condition = db_retrieve_table_daemon(ConditionalConditions).filter(
        ConditionalConditions.unique_id == condition_id).first()

    if not sql_condition:
        logger.error("Condition ID not found")
        return

    # Check Measurement Conditions
    if sql_condition.condition_type in ['measurement',
                                        'measurement_past_average',
                                        'measurement_past_sum']:
        device_id = sql_condition.measurement.split(',')[0]
        measurement_id = sql_condition.measurement.split(',')[1]

        device_measurement = db_retrieve_table_daemon(
            DeviceMeasurements, unique_id=measurement_id)
        if device_measurement:
            conversion = db_retrieve_table_daemon(
                Conversion, unique_id=device_measurement.conversion_id)
        else:
            conversion = None
        channel, unit, measurement = return_measurement_info(
            device_measurement, conversion)

        if None in [channel, unit]:
            logger.error(
                "Could not determine channel or unit from measurement ID: "
                "{}".format(measurement_id))
            return

        max_age = sql_condition.max_age

        if sql_condition.condition_type == 'measurement':
            influx_return = get_last_measurement(
                device_id, measurement_id, max_age=max_age)
            if influx_return is not None:
                return_measurement = influx_return[1]
            else:
                return_measurement = None
        elif sql_condition.condition_type == 'measurement_past_average':
            measurement_list = []
            past_measurements = get_past_measurements(
                device_id, measurement_id, max_age=max_age)
            for each_set in past_measurements:
                measurement_list.append(float(each_set[1]))
            return_measurement = sum(measurement_list) / len(measurement_list)
        elif sql_condition.condition_type == 'measurement_past_sum':
            measurement_list = []
            past_measurements = get_past_measurements(
                device_id, measurement_id, max_age=max_age)
            for each_set in past_measurements:
                measurement_list.append(float(each_set[1]))
            return_measurement = sum(measurement_list)
        else:
            return

        return return_measurement

    # Return GPIO state
    elif sql_condition.condition_type == 'gpio_state':
        try:
            import RPi.GPIO as GPIO
            GPIO.setmode(GPIO.BCM)
            GPIO.setup(int(sql_condition.gpio_pin), GPIO.IN)
            gpio_state = GPIO.input(int(sql_condition.gpio_pin))
        except Exception as e:
            gpio_state = None
            logger.error("Exception reading the GPIO pin: {}".format(e))
        return gpio_state

    # Return output state
    elif sql_condition.condition_type == 'output_state':
        output_id = sql_condition.output_id.split(",")[0]
        channel_id = sql_condition.output_id.split(",")[1]
        channel = db_retrieve_table_daemon(OutputChannel).filter(
            OutputChannel.unique_id == channel_id).first()
        control = DaemonControl()
        return control.output_state(output_id, output_channel=channel.channel)

    # Return the duration the output is currently on for
    elif sql_condition.condition_type == 'output_duration_on':
        output_id = sql_condition.output_id.split(",")[0]
        channel_id = sql_condition.output_id.split(",")[1]
        channel = db_retrieve_table_daemon(OutputChannel).filter(
            OutputChannel.unique_id == channel_id).first()
        control = DaemonControl()
        return control.output_sec_currently_on(output_id, output_channel=channel.channel)

    # Return controller active state
    elif sql_condition.condition_type == 'controller_status':
        control = DaemonControl()
        return control.controller_is_active(sql_condition.controller_id)


def get_condition_value_dict(condition_id):
    """
    Returns dict of multiple condition measurements for Conditional controllers
    :param condition_id: Conditional condition ID
    :return: measurement: dict of float measurements
    """
    # Check Measurement Conditions
    sql_condition = db_retrieve_table_daemon(ConditionalConditions).filter(
        ConditionalConditions.unique_id == condition_id).first()

    if sql_condition.condition_type == 'measurement_dict':
        device_id = sql_condition.measurement.split(',')[0]
        measurement_id = sql_condition.measurement.split(',')[1]
        max_age = sql_condition.max_age

        device_measurement = db_retrieve_table_daemon(
            DeviceMeasurements, unique_id=measurement_id)
        if device_measurement:
            conversion = db_retrieve_table_daemon(
                Conversion, unique_id=device_measurement.conversion_id)
        else:
            conversion = None
        channel, unit, measurement = return_measurement_info(
            device_measurement, conversion)

        if None in [channel, unit]:
            logger.error(
                "Could not determine channel or unit from measurement ID: "
                "{}".format(measurement_id))
            return

        past_measurements_dict = get_past_measurements(
            device_id, measurement_id, max_age=max_age)

        # TODO: Change to return dictionary in next major release
        string_ts_values = ''
        if past_measurements_dict:
            string_ts_values = ''
            for index, each_set in enumerate(past_measurements_dict):
                string_ts_values += '{},{}'.format(each_set[0], each_set[1])
                if index + 1 < len(past_measurements_dict):
                    string_ts_values += ';'

        return string_ts_values


def action_video(cond_action, message):
    this_camera = db_retrieve_table_daemon(
        Camera, unique_id=cond_action.do_unique_id, entry='first')
    message += "  Capturing video with camera {unique_id} ({id}, {name}).".format(
        unique_id=cond_action.do_unique_id,
        id=this_camera.id,
        name=this_camera.name)
    camera_stream = db_retrieve_table_daemon(
        Camera, unique_id=cond_action.do_unique_id)
    attachment_path_file = camera_record(
        'video', camera_stream.unique_id,
        duration_sec=cond_action.do_camera_duration)
    attachment_file = os.path.join(attachment_path_file[0], attachment_path_file[1])
    return message, attachment_file


def action_lcd_backlight_color(cond_action, message):
    control = DaemonControl()
    lcd = db_retrieve_table_daemon(
        LCD, unique_id=cond_action.do_unique_id)
    message += " LCD {unique_id} ({id}, {name}) Backlight Color to {color}.".format(
        unique_id=cond_action.do_unique_id,
        id=lcd.id,
        name=lcd.name,
        color=cond_action.do_action_string)

    lcd_color = threading.Thread(
        target=control.lcd_backlight_color,
        args=(cond_action.do_unique_id, cond_action.do_action_string,))
    lcd_color.start()
    return message


def trigger_action(
        function_actions,
        cond_action_id,
        value=None,
        message='',
        note_tags=None,
        email_recipients=None,
        attachment_file=None,
        attachment_type=None,
        single_action=False,
        debug=False):
    """
    Trigger individual action

    If single_action == False, message, note_tags, email_recipients,
    attachment_file, and attachment_type are returned and may be
    passed back to this function in order to append to those lists.

    :param function_actions: dict of function action information
    :param cond_action_id: unique_id of action
    :param value: a variable to be sent to the action
    :param message: message string to append to that will be sent back
    :param note_tags: list of note tags to use if creating a note
    :param email_recipients: list of email addresses to notify if sending an email
    :param attachment_file: string location of a file to attach to an email
    :param attachment_type: string type of email attachment
    :param single_action: True if only one action is being triggered, False if only one of multiple actions
    :param debug: determine if logging level should be DEBUG

    :return: message or (message, note_tags, email_recipients, attachment_file, attachment_type)
    """
    cond_action = db_retrieve_table_daemon(Actions, unique_id=cond_action_id)
    if not cond_action:
        message += 'Error: Action with ID {} not found!'.format(
            cond_action_id)
        if single_action:
            return message
        else:
            return (message,
                    note_tags,
                    email_recipients,
                    attachment_file,
                    attachment_type)

    logger_actions = logging.getLogger("mycodo.trigger_action_{id}".format(
        id=cond_action.unique_id.split('-')[0]))

    if debug:
        logger_actions.setLevel(logging.DEBUG)
    else:
        logger_actions.setLevel(logging.INFO)

    # Set up function action to run from standalone action module file
    run_function_action = None
    if cond_action.action_type in function_actions:
        dict_vars = {"value": value}

        function_action_loaded = load_module_from_file(
            function_actions[cond_action.action_type]['file_path'], 'function_action')
        if function_action_loaded:
            run_function_action = function_action_loaded.ActionModule(cond_action)

        # Run action
        message = run_function_action.run_action(message, dict_vars)

        message += "\n[Action {id}, {name}]:".format(
            id=cond_action.unique_id.split('-')[0],
            name=function_actions[cond_action.action_type]['name'])

    logger_actions.debug("Message: {}".format(message))
    logger_actions.debug("Note Tags: {}".format(note_tags))
    logger_actions.debug("Email Recipients: {}".format(email_recipients))
    logger_actions.debug("Attachment Files: {}".format(attachment_file))
    logger_actions.debug("Attachment Type: {}".format(attachment_type))

    if single_action:
        return message
    else:
        return (message, note_tags, email_recipients,
                attachment_file, attachment_type)


def trigger_function_actions(function_actions, function_id, message='', debug=False):
    """
    Execute the Actions belonging to a particular Function

    :param function_actions: dict of function action information
    :param function_id: unique ID of function to execute all actions of
    :param message: The message generated from the conditional check
    :param debug: determine if logging level should be DEBUG
    :return:
    """
    logger_actions = logging.getLogger("mycodo.trigger_function_actions_{id}".format(
        id=function_id.split('-')[0]))

    if debug:
        logger_actions.setLevel(logging.DEBUG)
    else:
        logger_actions.setLevel(logging.INFO)

    # List of all email notification recipients
    # List is appended with TO email addresses when an email Action is
    # encountered. An email is sent to all recipients after all actions
    # have been executed.
    email_recipients = []

    # List of tags to add to a note
    note_tags = []

    attachment_file = None
    attachment_type = None

    actions = db_retrieve_table_daemon(Actions)
    actions = actions.filter(
        Actions.function_id == function_id).all()

    for cond_action in actions:
        if cond_action.action_type == 'mqtt_publish':
            continue  # Actions to skip when triggering all actions
                      # TODO: incorporate into single-file module when refactored

        (message,
         note_tags,
         email_recipients,
         attachment_file,
         attachment_type) = trigger_action(
            function_actions,
            cond_action.unique_id,
            message=message,
            single_action=False,
            note_tags=note_tags,
            email_recipients=email_recipients,
            attachment_file=attachment_file,
            attachment_type=attachment_type,
            debug=debug)

    # Send email after all conditional actions have been checked
    # In order to append all action messages to send in the email
    # send_email_at_end will be None or the TO email address
    if email_recipients:
        # If the emails per hour limit has not been exceeded
        smtp_wait_timer, allowed_to_send_notice = check_allowed_to_email()
        if allowed_to_send_notice:
            smtp = db_retrieve_table_daemon(SMTP, entry='first')
            send_email(smtp.host, smtp.protocol, smtp.port,
                       smtp.user, smtp.passw, smtp.email_from,
                       email_recipients, message,
                       attachment_file, attachment_type)
        else:
            logger_actions.error("Wait {sec:.0f} seconds to email again.".format(
                sec=smtp_wait_timer - time.time()))

    # Create a note with the tags from the unique_ids in the list note_tags
    if note_tags:
        list_tags = []
        for each_note_tag_id in note_tags:
            check_tag = db_retrieve_table_daemon(
                NoteTags, unique_id=each_note_tag_id)
            if check_tag:
                list_tags.append(each_note_tag_id)
        if list_tags:
            with session_scope(MYCODO_DB_PATH) as db_session:
                new_note = Notes()
                new_note.name = 'Action'
                new_note.tags = ','.join(list_tags)
                new_note.note = message
                db_session.add(new_note)

    logger_actions.debug("Message: {}".format(message))
    return message


def which_controller(unique_id):
    """Determine which type of controller the unique_id is for"""
    controller_type = None
    controller_object = None
    controller_entry = None

    if db_retrieve_table_daemon(Conditional, unique_id=unique_id):
        controller_type = 'Conditional'
        controller_object = Conditional
        controller_entry = db_retrieve_table_daemon(
            Conditional, unique_id=unique_id)
    elif db_retrieve_table_daemon(CustomController, unique_id=unique_id):
        controller_type = 'Function'
        controller_object = CustomController
        controller_entry = db_retrieve_table_daemon(
            CustomController, unique_id=unique_id)
    elif db_retrieve_table_daemon(Input, unique_id=unique_id):
        controller_type = 'Input'
        controller_object = Input
        controller_entry = db_retrieve_table_daemon(
            Input, unique_id=unique_id)
    elif db_retrieve_table_daemon(LCD, unique_id=unique_id):
        controller_type = 'LCD'
        controller_object = LCD
        controller_entry = db_retrieve_table_daemon(
            LCD, unique_id=unique_id)
    elif db_retrieve_table_daemon(Math, unique_id=unique_id):
        controller_type = 'Math'
        controller_object = Math
        controller_entry = db_retrieve_table_daemon(
            Math, unique_id=unique_id)
    elif db_retrieve_table_daemon(PID, unique_id=unique_id):
        controller_type = 'PID'
        controller_object = PID
        controller_entry = db_retrieve_table_daemon(
            PID, unique_id=unique_id)
    elif db_retrieve_table_daemon(Trigger, unique_id=unique_id):
        controller_type = 'Trigger'
        controller_object = Trigger
        controller_entry = db_retrieve_table_daemon(
            Trigger, unique_id=unique_id)

    return controller_type, controller_object, controller_entry
