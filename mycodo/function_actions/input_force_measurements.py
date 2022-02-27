# coding=utf-8
import threading

from flask_babel import lazy_gettext

from mycodo.databases.models import Actions
from mycodo.databases.models import Input
from mycodo.function_actions.base_function_action import AbstractFunctionAction
from mycodo.utils.database import db_retrieve_table_daemon

FUNCTION_ACTION_INFORMATION = {
    'name_unique': 'input_force_measurements',
    'name': "{}: {}".format(
        lazy_gettext('Input'), lazy_gettext('Force Measurements')),
    'library': None,
    'manufacturer': 'Mycodo',

    'url_manufacturer': None,
    'url_datasheet': None,
    'url_product_purchase': None,
    'url_additional': None,

    'message': lazy_gettext('Force measurements to be conducted for an input'),

    'usage': 'Executing <strong>self.run_action("{ACTION_ID}")</strong> will force acquiring measurements for the selected Input. '
             'Executing <strong>self.run_action("{ACTION_ID}", value={"input_id": "959019d1-c1fa-41fe-a554-7be3366a9c5b"})</strong> will force acquiring measurements for the Input with the specified ID (e.g. 959019d1-c1fa-41fe-a554-7be3366a9c5b),',

    'dependencies_module': [],

    'custom_options': [
        {
            'id': 'input',
            'type': 'select_device',
            'default_value': '',
            'required': False,
            'options_select': [
                'Input'
            ],
            'name': 'Input',
            'phrase': 'Select an Input'
        }
    ]
}


class ActionModule(AbstractFunctionAction):
    """
    Function Action: Force Input Measurements
    """
    def __init__(self, action_dev, testing=False):
        super(ActionModule, self).__init__(action_dev, testing=testing, name=__name__)

        self.input_id = None

        action = db_retrieve_table_daemon(
            Actions, unique_id=self.unique_id)
        self.setup_custom_options(
            FUNCTION_ACTION_INFORMATION['custom_options'], action)

        if not testing:
            self.setup_action()

    def setup_action(self):
        self.action_setup = True

    def run_action(self, message, dict_vars):
        try:
            input_id = dict_vars["value"]["input_id"]
        except:
            input_id = self.input_id

        this_input = db_retrieve_table_daemon(
            Input, unique_id=input_id, entry='first')

        if not this_input:
            msg = "Input not found with ID {}".format(input_id)
            message += msg
            self.logger.error(msg)
            return

        message += " Force measuring from Input {unique_id} ({id}, {name}).".format(
            unique_id=input_id,
            id=this_input.id,
            name=this_input.name)
        force_measurements = threading.Thread(
            target=self.control.input_force_measurements,
            args=(input_id,))
        force_measurements.start()

        return message

    def is_setup(self):
        return self.action_setup
