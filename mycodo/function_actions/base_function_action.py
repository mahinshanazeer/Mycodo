# coding=utf-8
"""
This module contains the AbstractFunctionAction Class which acts as a template
for all outputs. It is not to be used directly. The AbstractFunctionAction Class
ensures that certain methods and instance variables are included in each
Output.

All Outputs should inherit from this class and overwrite methods that raise
NotImplementedErrors
"""
import logging
import timeit

from mycodo.abstract_base_controller import AbstractBaseController
from mycodo.config import SQL_DATABASE_MYCODO
from mycodo.databases.models import Trigger
from mycodo.databases.models import Conditional
from mycodo.databases.models import Function
from mycodo.databases.utils import session_scope
from mycodo.mycodo_client import DaemonControl
from mycodo.utils.lockfile import LockFile

MYCODO_DB_PATH = 'sqlite:///' + SQL_DATABASE_MYCODO

class AbstractFunctionAction(AbstractBaseController):
    """
    Base Function Action class that ensures certain methods and values are present
    in Function Actions.
    """
    def __init__(self, action_dev, testing=False, name=__name__):
        if not testing:
            super(AbstractFunctionAction, self).__init__(action_dev.unique_id, testing=testing, name=__name__)
        else:
            super(AbstractFunctionAction, self).__init__(None, testing=testing, name=__name__)

        self.action = action_dev
        self.action_setup = False
        self.startup_timer = timeit.default_timer()
        self.control = DaemonControl()

        self.logger = None
        self.setup_logger(testing=testing, name=name, action=action_dev)

        self.running = True

        if not testing:
            self.unique_id = action_dev.unique_id

    def __iter__(self):
        """ Support the iterator protocol """
        return self

    def __repr__(self):
        """  Representation of object """
        return_str = '<{cls}'.format(cls=type(self).__name__)
        return_str += '>'
        return return_str

    def __str__(self):
        """ Return measurement information """
        return_str = ''
        return return_str

    def is_setup(self):
        self.logger.error(
            "{cls} did not overwrite the is_setup() method. All "
            "subclasses of the AbstractFunctionAction class are required to overwrite "
            "this method".format(cls=type(self).__name__))
        raise NotImplementedError

    def setup_action(self):
        self.logger.error(
            "{cls} did not overwrite the setup_output() method. All "
            "subclasses of the AbstractFunctionAction class are required to overwrite "
            "this method".format(cls=type(self).__name__))
        raise NotImplementedError

    def run_action(self, message, dict_vars):
        """ Called when Action is executed """
        pass

    #
    # Do not overwrite the function below
    #

    def setup_logger(self, testing=None, name=None, action=None):
        name = name if name else __name__
        if not testing and action:
            log_name = "{}_{}".format(name, action.unique_id.split('-')[0])
        else:
            log_name = name
        self.logger = logging.getLogger(log_name)
        if not testing and action:
            with session_scope(MYCODO_DB_PATH) as new_session:
                debug_level = logging.INFO

                conditional = new_session.query(Conditional).filter(
                    Conditional.unique_id == action.function_id).first()
                if conditional and conditional.log_level_debug:
                    debug_level = logging.DEBUG

                function = new_session.query(Function).filter(
                    Function.unique_id == action.function_id).first()
                if function and function.log_level_debug:
                    debug_level = logging.DEBUG

                trigger = new_session.query(Trigger).filter(
                    Trigger.unique_id == action.function_id).first()
                if trigger and trigger.log_level_debug:
                    debug_level = logging.DEBUG

                self.logger.error("TEST: {}, {}".format(action.function_id, function))

                self.logger.setLevel(debug_level)
