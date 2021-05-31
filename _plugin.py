# coding: utf-8
# OceanBase Deploy.
# Copyright (C) 2021 OceanBase
#
# This file is part of OceanBase Deploy.
#
# OceanBase Deploy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# OceanBase Deploy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OceanBase Deploy.  If not, see <https://www.gnu.org/licenses/>.


from __future__ import absolute_import, division, print_function

import os
import sys
from enum import Enum
from glob import glob
from copy import deepcopy

from _manager import Manager
from tool import ConfigUtil, DynamicLoading, YamlLoader


yaml = YamlLoader()


class PluginType(Enum):

    START = 'StartPlugin'
    PARAM = 'ParamPlugin'
    INSTALL = 'InstallPlugin'
    PY_SCRIPT = 'PyScriptPlugin'


class Plugin(object):
    
    PLUGIN_TYPE = None
    FLAG_FILE = None

    def __init__(self, component_name, plugin_path, version):
        if not self.PLUGIN_TYPE or not self.FLAG_FILE:
            raise NotImplementedError
        self.component_name = component_name
        self.plugin_path = plugin_path
        self.version = version.split('.')

    def __str__(self):
        return '%s-%s-%s' % (self.component_name, self.PLUGIN_TYPE.name.lower(), '.'.join(self.version))

    @property
    def mirror_type(self):
        return self.PLUGIN_TYPE


class PluginReturn(object):

    def __init__(self, value=False, *arg, **kwargs):
        self._return_value = value
        self._return_args = arg
        self._return_kwargs = kwargs

    def __nonzero__(self):
        return self.__bool__()

    def __bool__(self):
        return True if self._return_value else False

    @property
    def value(self):
        return self._return_value
    
    @property
    def args(self):
        return self._return_args

    @property
    def kwargs(self):
        return self._return_kwargs
    
    def get_return(self, key):
        if key in self.kwargs:
            return self.kwargs[key]
        return None

    def set_args(self, *args):
        self._return_args = args

    def set_kwargs(self, **kwargs):
        self._return_kwargs = kwargs

    def set_return(self, value):
        self._return_value = value
    
    def return_true(self, *args, **kwargs):
        self.set_return(True)
        self.set_args(*args)
        self.set_kwargs(**kwargs)
    
    def return_false(self, *args, **kwargs):
        self.set_return(False)
        self.set_args(*args)
        self.set_kwargs(**kwargs)


class PluginContext(object):

    def __init__(self, components, clients, cluster_config, cmd, options, stdio):
        self.components = components
        self.clients = clients
        self.cluster_config = cluster_config
        self.cmd = cmd
        self.options = options
        self.stdio = stdio
        self._return = PluginReturn()

    def get_return(self):
        return self._return

    def return_true(self, *args, **kwargs):
        self._return.return_true(*args, **kwargs)
    
    def return_false(self, *args, **kwargs):
        self._return.return_false(*args, **kwargs)


class SubIO(object):

    def __init__(self, stdio):
        self.stdio = getattr(stdio, 'sub_io', lambda: None)()
        self._func = {}

    def __del__(self):
        self.before_close()
    
    def _temp_function(self, *arg, **kwargs):
        pass

    def __getattr__(self, name):
        if name not in self._func:
            self._func[name] = getattr(self.stdio, name, self._temp_function)
        return self._func[name]


class ScriptPlugin(Plugin):

    class ClientForScriptPlugin(object):

        def __init__(self, client, stdio):
            self.client = client
            self.stdio = stdio

        def __getattr__(self, key):
            def new_method(*args, **kwargs):
                kwargs['stdio'] = self.stdio
                return attr(*args, **kwargs)
            attr = getattr(self.client, key)
            if hasattr(attr, '__call__'):
                return new_method
            return attr

    def __init__(self, component_name, plugin_path, version):
        super(ScriptPlugin, self).__init__(component_name, plugin_path, version)
        self.context = None

    def __call__(self):
        raise NotImplementedError

    def _import(self, stdio=None):
        raise NotImplementedError

    def _export(self):
        raise NotImplementedError

    def __del__(self):
        self._export()

    def before_do(self, components, clients, cluster_config, cmd, options, stdio, *arg, **kwargs):
        self._import(stdio)
        sub_stdio = SubIO(stdio)
        sub_clients = {}
        for server in clients:
            sub_clients[server] = ScriptPlugin.ClientForScriptPlugin(clients[server], sub_stdio)
        self.context = PluginContext(components, sub_clients, cluster_config, cmd, options, sub_stdio)

    def after_do(self, stdio, *arg, **kwargs):
        self._export(stdio)
        self.context = None


def pyScriptPluginExec(func):
    def _new_func(self, components, clients, cluster_config, cmd, options, stdio, *arg, **kwargs):
        self.before_do(components, clients, cluster_config, cmd, options, stdio, *arg, **kwargs)
        if self.module:
            method_name = func.__name__
            method = getattr(self.module, method_name, False)
            if method:
                try:
                    method(self.context, *arg, **kwargs)
                except Exception as e:
                    stdio and getattr(stdio, 'exception', print)('%s RuntimeError: %s' % (self, e))
                    pass
        ret = self.context.get_return() if self.context else PluginReturn()
        self.after_do(stdio, *arg, **kwargs)
        return ret
    return _new_func


class PyScriptPlugin(ScriptPlugin):

    LIBS_PATH = []
    PLUGIN_COMPONENT_NAME = None

    def __init__(self, component_name, plugin_path, version):
        if not self.PLUGIN_COMPONENT_NAME:
            raise NotImplementedError
        super(PyScriptPlugin, self).__init__(component_name, plugin_path, version)
        self.module = None
        self.libs_path = deepcopy(self.LIBS_PATH)
        self.libs_path.append(self.plugin_path)

    def __call__(self, clients, cluster_config, cmd, options, stdio, *arg, **kwargs):
        method = getattr(self, self.PLUGIN_COMPONENT_NAME, False)
        if method:
            return method(clients, cluster_config, cmd, options, stdio, *arg, **kwargs)
        else:
            raise NotImplementedError

    def _import(self, stdio=None):
        if self.module is None:
            DynamicLoading.add_libs_path(self.libs_path)
            self.module = DynamicLoading.import_module(self.PLUGIN_COMPONENT_NAME, stdio)

    def _export(self, stdio=None):
        if self.module:
            DynamicLoading.remove_libs_path(self.libs_path)
            DynamicLoading.export_module(self.PLUGIN_COMPONENT_NAME, stdio)

# this is PyScriptPlugin demo
# class InitPlugin(PyScriptPlugin):

#     FLAG_FILE = 'init.py'
#     PLUGIN_COMPONENT_NAME = 'init'
#     PLUGIN_TYPE = PluginType.INIT

#     def __init__(self, component_name, plugin_path, version):
#         super(InitPlugin, self).__init__(component_name, plugin_path, version)

#     @pyScriptPluginExec
#     def init(self, components, ssh_clients, cluster_config, cmd, options, stdio, *arg, **kwargs):
#         pass


class ParamPlugin(Plugin):

    class ConfigItem(object):

        def __init__(self, name, default=None, require=False, need_restart=False, need_redeploy=False):
            self.name = name
            self.default = default
            self.require = require
            self.need_restart = need_restart
            self.need_redeploy = need_redeploy

    PLUGIN_TYPE = PluginType.PARAM
    DEF_PARAM_YAML = 'parameter.yaml'
    FLAG_FILE = DEF_PARAM_YAML

    def __init__(self, component_name, plugin_path, version):
        super(ParamPlugin, self).__init__(component_name, plugin_path, version)
        self.def_param_yaml_path = os.path.join(self.plugin_path, self.DEF_PARAM_YAML)
        self._src_data = None

    @property
    def params(self):
        if self._src_data is None:
            try:
                self._src_data = {}
                with open(self.def_param_yaml_path, 'rb') as f:
                    configs = yaml.load(f)
                    for conf in configs:
                        self._src_data[conf['name']] = ParamPlugin.ConfigItem(
                            conf['name'], 
                            ConfigUtil.get_value_from_dict(conf, 'default', None),
                            ConfigUtil.get_value_from_dict(conf, 'require', False),
                            ConfigUtil.get_value_from_dict(conf, 'need_restart', False),
                            ConfigUtil.get_value_from_dict(conf, 'need_redeploy', False),
                        )
            except:
                pass
        return self._src_data

    def get_need_redeploy_items(self):
        items = []
        params = self.params
        for name in params:
            conf = params[name]
            if conf.need_redeploy:
                items.append(name)
        return items

    def get_need_restart_items(self):
        items = []
        params = self.params
        for name in params:
            conf = params[name]
            if conf.need_restart:
                items.append(name)
        return items

    def get_params_default(self):
        temp = {}
        params = self.params
        for name in params:
            conf = params[name]
            temp[conf.name] = conf.default
        return temp


class InstallPlugin(Plugin):

    class FileItem(object):

        def __init__(self, src_path, target_path, _type):
            self.src_path = src_path
            self.target_path = target_path
            self.type = _type if _type else 'file'

    PLUGIN_TYPE = PluginType.INSTALL
    FILES_MAP_YAML = 'file_map.yaml'
    FLAG_FILE = FILES_MAP_YAML

    def __init__(self, component_name, plugin_path, version):
        super(InstallPlugin, self).__init__(component_name, plugin_path, version)
        self.file_map_path = os.path.join(self.plugin_path, self.FILES_MAP_YAML)
        self._file_map = None

    @property
    def file_map(self):
        if self._file_map is None:
            try:
                self._file_map = {}
                with open(self.file_map_path, 'rb') as f:
                    file_map = yaml.load(f)
                    for data in file_map:
                        k = data['src_path']
                        if k[0] != '.':
                            k = '.%s' % os.path.join('/', k)
                        self._file_map[k] = InstallPlugin.FileItem(
                            k,
                            ConfigUtil.get_value_from_dict(data, 'target_path', k),
                            ConfigUtil.get_value_from_dict(data, 'type', None)
                        )
            except:
                pass
        return self._file_map

    def file_list(self):
        file_map = self.file_map
        return [file_map[k] for k in file_map]



class ComponentPluginLoader(object):

    PLUGIN_TYPE = None

    def __init__(self, home_path, plugin_type=PLUGIN_TYPE, stdio=None):
        if plugin_type:
            self.PLUGIN_TYPE = plugin_type
        if not self.PLUGIN_TYPE:
            raise NotImplementedError
        self.plguin_cls = getattr(sys.modules[__name__], self.PLUGIN_TYPE.value, False)
        if not self.plguin_cls:
            raise ImportError(self.PLUGIN_TYPE.value)
        self.stdio = stdio
        self.path = home_path
        self.component_name = os.path.split(self.path)[1]
        self._plugins = {}

    def get_plugins(self):
        plugins = []
        for flag_path in glob('%s/*/%s' % (self.path, self.plguin_cls.FLAG_FILE)):
            if flag_path in self._plugins:
                plugins.append(self._plugins[flag_path])
            else:
                path, _ = os.path.split(flag_path)
                _, version = os.path.split(path)
                plugin = self.plguin_cls(self.component_name, path, version)
                self._plugins[flag_path] = plugin
                plugins.append(plugin)
        return plugins

    def get_best_plugin(self, version):
        version = version.split('.')
        plugins = []
        for plugin in self.get_plugins():
            if plugin.version == version:
                return plugin
            if plugin.version < version:
                plugins.append(plugin)
        if plugins:
            plugin = max(plugins, key=lambda x: x.version)
            self.stdio and getattr(self.stdio, 'warn', print)(
                '%s %s plugin version %s not found, use the best suitable version %s\n. Use `obd update` to update local plugin repository' % 
                (self.component_name, self.PLUGIN_TYPE.name.lower(), '.'.join(version), '.'.join(plugin.version))
                )
            return plugin
        return None


class PyScriptPluginLoader(ComponentPluginLoader):

    class PyScriptPluginType(object):

        def __init__(self, name, value):
            self.name = name
            self.value = value

    PLUGIN_TYPE = PluginType.PY_SCRIPT

    def __init__(self, home_path, script_name=None, stdio=None):
        if not script_name:
            raise NotImplementedError
        type_name = 'PY_SCRIPT_%s' % script_name.upper()
        type_value = 'PyScript%sPlugin' % ''.join([word.capitalize() for word in script_name.split('_')])
        self.PLUGIN_TYPE = PyScriptPluginLoader.PyScriptPluginType(type_name, type_value)
        if not getattr(sys.modules[__name__], type_value, False):
            self._create_(script_name)
        super(PyScriptPluginLoader, self).__init__(home_path, stdio=stdio)

    def _create_(self, script_name):
        exec('''
class %s(PyScriptPlugin):

    FLAG_FILE = '%s.py'
    PLUGIN_COMPONENT_NAME = '%s'

    def __init__(self, component_name, plugin_path, version):
        super(%s, self).__init__(component_name, plugin_path, version)

    @staticmethod
    def set_plugin_type(plugin_type):
        %s.PLUGIN_TYPE = plugin_type

    @pyScriptPluginExec
    def %s(self, components, ssh_clients, cluster_config, cmd, options, stdio, *arg, **kwargs):
        pass
        ''' % (self.PLUGIN_TYPE.value, script_name, script_name, self.PLUGIN_TYPE.value, self.PLUGIN_TYPE.value, script_name))
        clz = locals()[self.PLUGIN_TYPE.value]
        setattr(sys.modules[__name__], self.PLUGIN_TYPE.value, clz)
        clz.set_plugin_type(self.PLUGIN_TYPE)
        return clz


class PluginManager(Manager):

    RELATIVE_PATH = 'plugins'
    # The directory structure for plugin is ./plugins/{component_name}/{version}

    def __init__(self, home_path, stdio=None):
        super(PluginManager, self).__init__(home_path, stdio=stdio)
        self.component_plugin_loaders = {}
        self.py_script_plugin_loaders = {}
        for plugin_type in PluginType:
            self.component_plugin_loaders[plugin_type] = {}
        # PyScriptPluginLoader is a customized script loader. It needs special processing.
        # Log off the PyScriptPluginLoader in component_plugin_loaders
        del self.component_plugin_loaders[PluginType.PY_SCRIPT]

    def get_best_plugin(self, plugin_type, component_name, version):
        if plugin_type not in self.component_plugin_loaders:
            return None
        loaders = self.component_plugin_loaders[plugin_type]
        if component_name not in loaders:
            loaders[component_name] = ComponentPluginLoader(os.path.join(self.path, component_name), plugin_type, self.stdio)
        loader = loaders[component_name]
        return loader.get_best_plugin(version)

    # 主要用于获取自定义Python脚本插件
    # 相比于get_best_plugin，该方法可以获取到未在PluginType中注册的Python脚本插件
    # 这个功能可以快速实现自定义插件，只要在插件仓库创建对应的python文件，并暴露出同名方法即可
    # 使后续进一步实现全部流程可描述更容易实现
    def get_best_py_script_plugin(self, script_name, component_name, version):
        if script_name not in self.py_script_plugin_loaders:
            self.py_script_plugin_loaders[script_name] = {}
        loaders = self.py_script_plugin_loaders[script_name]
        if component_name not in loaders:
            loaders[component_name] = PyScriptPluginLoader(os.path.join(self.path, component_name), script_name, self.stdio)
        loader = loaders[component_name]
        return loader.get_best_plugin(version)