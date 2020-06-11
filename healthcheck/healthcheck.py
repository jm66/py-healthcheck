#!/usr/bin/env python
# -*- coding: utf-8 -*-
import json
import logging
import socket
import time
from functools import reduce, wraps
from typing import AnyStr, Union

import six

try:
    from collections.abc import Iterable
except ImportError:
    from collections import Iterable

from .timeout import timeout

logger = logging.getLogger(__name__)


def basic_exception_handler(_, e):
    return False, str(e)


def json_success_handler(results, *args, **kw):
    data = {
        'hostname': socket.gethostname(),
        'status': 'success',
        'timestamp': time.time(),
        'results': results,
    }
    [data.update({k: v}) for k, v in kw.items()]
    return json.dumps(data)


def json_failed_handler(results, *args, **kw):
    data = {
        'hostname': socket.gethostname(),
        'status': 'failure',
        'timestamp': time.time(),
        'results': results,
    }
    [data.update({k: v}) for k, v in kw.items()]
    return json.dumps(data)


def check_reduce(passed, result):
    return passed and result.get('passed')


class HealthCheckMonitor(object):
    checkers = {}

    @classmethod
    def unregister_all(cls):
        # type: () -> None
        cls.checkers = {}

    @classmethod
    def register(cls, checker):
        # type: (Union[Checker, callable]) -> None
        if isinstance(checker, Checker):
            cls.checkers[checker.name] = checker
            return
        cls.checkers[checker.__name__] = checker

    @classmethod
    def get_checkers(cls, name=None):
        # type: (AnyStr) -> Iterable[Checker]
        if name:
            return list(filter(None, [cls.get(name)]))
        return cls.checkers.values()

    @classmethod
    def get(cls, name):
        # type: (AnyStr) -> Checker
        return cls.checkers.get(name)


class Checker:
    def __init__(self, name=None):
        self._name = name

    def __call__(self, *args, **kwargs):
        return self.call(*args, **kwargs)

    @property
    def name(self):
        return self._name

    def decorate(self, function):
        if self._name is None:
            self._name = function.__name__
            # self._name = function.__qualname__

        HealthCheckMonitor.register(self)

        @wraps(function)
        def wrapper(*args, **kwargs):
            return self._call(function, *args, **kwargs)

        self._wrapped = wrapper
        return wrapper

    def call(self, *args, **kwargs):
        return self._wrapped(*args, **kwargs)

    def _call(self, func, *args, **kwargs):
        return func(*args, **kwargs)


class HealthCheck(object):
    def __init__(self, success_status=200,
                 success_headers=None, success_handler=json_success_handler,
                 success_ttl=27, failed_status=500, failed_headers=None,
                 failed_handler=json_failed_handler, failed_ttl=9,
                 error_timeout=0,
                 exception_handler=basic_exception_handler, checkers=None,
                 **kwargs):
        self.cache = dict()

        self.success_status = success_status
        self.success_headers = success_headers or {'Content-Type': 'application/json'}
        self.success_handler = success_handler
        self.success_ttl = float(success_ttl or 0)

        self.failed_status = failed_status
        self.failed_headers = failed_headers or {'Content-Type': 'application/json'}
        self.failed_handler = failed_handler
        self.failed_ttl = float(failed_ttl or 0)

        self.error_timeout = error_timeout

        self.exception_handler = exception_handler

        if checkers:
            [self.add_check(c) for c in checkers]

        self.functions = dict()
        # ads custom_sections on signature
        [self.add_section(k, v) for k, v in kwargs.items() if k not in self.functions]

    def add_section(self, name, func):
        if name in self.functions:
            raise Exception('The name "{}" is already taken.'.format(name))
        self.functions[name] = func

    def add_check(self, func):
        if not isinstance(func, Checker):
            Checker().decorate(func)
            return
        HealthCheckMonitor.register(func)

    def run(self, check=None):
        results = []
        filtered = HealthCheckMonitor.get_checkers(check)
        for checker in filtered:
            if checker in self.cache and self.cache[checker].get('expires') >= time.time():
                result = self.cache[checker]
            else:
                result = self.run_check(checker)
                self.cache[checker] = result
            results.append(result)

        custom_section = dict()
        for (name, func) in six.iteritems(self.functions):
            try:
                custom_section[name] = func() if callable(func) else func
            except Exception:
                pass

        passed = reduce(check_reduce, results, True)

        if passed:
            message = 'OK'
            if self.success_handler:
                message = self.success_handler(results, **custom_section)

            return message, self.success_status, self.success_headers
        else:
            message = 'NOT OK'
            if self.failed_handler:
                message = self.failed_handler(results, **custom_section)

            return message, self.failed_status, self.failed_headers

    def run_check(self, checker):
        start_time = time.time()

        try:
            if self.error_timeout > 0:
                passed, output = timeout(self.error_timeout, 'Timeout error!')(checker)()
            else:
                passed, output = checker()
        except Exception as e:
            logger.exception(e)
            passed, output = self.exception_handler(checker, e)

        end_time = time.time()
        elapsed_time = end_time - start_time
        # Reduce to 6 decimal points to have consistency with timestamp
        elapsed_time = float('{:.6f}'.format(elapsed_time))

        if not passed:
            logger.error('Health check "%s" failed with output "%s"', checker.name, output)

        timestamp = time.time()
        if passed:
            expires = timestamp + self.success_ttl
        else:
            expires = timestamp + self.failed_ttl

        result = {'checker': checker.name,
                  'output': output,
                  'passed': passed,
                  'timestamp': timestamp,
                  'expires': expires,
                  'response_time': elapsed_time}
        return result


def checker(name=None):
    # if the decorator is used without parameters, the
    # wrapped function is provided as first argument
    if callable(name):
        return Checker().decorate(name)

    return Checker(name=name).decorate
