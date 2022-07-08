#!/usr/bin/env python
# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved

"""
Config command to setup the CLI and settings.
"""

import argparse
from dataclasses import is_dataclass, Field, fields, MISSING
from enum import Enum
import itertools
import json
import logging
import os
import typing as t

from threatexchange.fb_threatexchange.api import ThreatExchangeAPI


try:
    from typing import ForwardRef  # >= 3.7
except ImportError:
    # <3.7
    from typing import _ForwardRef as ForwardRef  # type: ignore


from threatexchange.extensions.manifest import ThreatExchangeExtensionManifest
from threatexchange import meta as tx_meta
from threatexchange import common
from threatexchange.cli.cli_config import CLISettings
from threatexchange.cli import command_base
from threatexchange.cli.exceptions import CommandError
from threatexchange.fetcher.apis.fb_threatexchange_api import (
    FBThreatExchangeCollabConfig,
    FBThreatExchangeSignalExchangeAPI,
)
from threatexchange.fetcher.fetch_api import SignalExchangeAPI
from threatexchange.fetcher.apis.ncmec_api import NCMECSignalExchangeAPI
from threatexchange.fetcher.apis.static_sample import StaticSampleSignalExchangeAPI
from threatexchange.signal_type.signal_base import SignalType


class ConfigCollabListCommand(command_base.Command):
    """List collaborations"""

    @classmethod
    def get_name(cls) -> str:
        return "list"

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        # Ideas:
        #  * Filter by API
        pass

    def execute(self, settings: CLISettings) -> None:
        for collab in settings.get_all_collabs(default_to_sample=False):
            api = settings.apis.get_for_collab(collab)
            print(api.get_name(), collab.name)


class _UpdateCollabCommand(command_base.Command):
    """
    Create or edit collaborations for this API

    Programatically generated by inspecting the config class, so not everything will be
    documented.
    """

    _API_CLS: t.ClassVar[t.Type[SignalExchangeAPI]]

    _IGNORE_FIELDS = {
        "name",
        "api",
        "enabled",
        # "only_signal_types",
        # "not_signal_types",
        # "only_owners",
        # "not_owners",
        # "only_tags",
        # "not_tags",
    }

    @classmethod
    def get_name(cls) -> str:
        return cls._API_CLS.get_name()

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        cfg_cls = cls._API_CLS.get_config_class()
        assert is_dataclass(cfg_cls)

        ap.add_argument("collab_name", help="the name of the collab")
        ap.set_defaults(api_name=cls._API_CLS.get_name())
        ap.add_argument(
            "--create",
            "-C",
            action="store_true",
            help="indicate you intend to create a config",
        )

        cfg_common_ap = ap.add_argument_group(description="common to all collabs")
        on_off = cfg_common_ap.add_mutually_exclusive_group()

        # This goofy syntax allows --enable, --enable=1, and enable=0 to disable
        on_off.add_argument(
            "--enable",
            nargs="?",
            type=int,
            const=1,
            choices=[0, 1],
            help="enable the config (default on create)",
        )
        on_off.add_argument(
            "--disable",
            dest="enable",
            action="store_const",
            const=0,
            help="disable the config",
        )

        type_specific_fields = [f for f in fields(cfg_cls) if cls._is_argument_field(f)]
        if type_specific_fields:
            config_ap = ap.add_argument_group(
                description=f"specific to {cls._API_CLS.get_name()}"
            )
            for field in type_specific_fields:
                cls._add_argument(config_ap, field)

        ap.add_argument(
            "--json",
            "-J",
            dest="is_json",
            action="store_true",
            help="instead, interpret the argument as JSON and use that to edit the config",
        )

    @classmethod
    def _is_argument_field(cls, field: Field) -> bool:
        if not field.init:
            return False
        if field.name in cls._IGNORE_FIELDS:
            return False
        return True

    @classmethod
    def _add_argument(cls, ap: argparse._ArgumentGroup, field: Field) -> None:
        assert cls._is_argument_field(field)
        assert not isinstance(
            field.type, ForwardRef
        ), "rework class to not have forward ref"

        target_type = field.type
        if hasattr(field.type, "__args__"):
            target_type = field.type.__args__[0]

        argparse_type = target_type
        metavar = target_type.__name__

        if issubclass(target_type, Enum):
            argparse_type = common.argparse_choices_pre_type(
                [m.name for m in target_type],
                lambda s: target_type[s],
            )
            metavar = f"[{','.join(m.name for m in target_type)}]"

        help = "[missing] Add a help annotation on the config class!"
        if field.metadata:
            metavar = field.metadata.get("metavar", metavar)
            help = field.metadata.get("help", help)

        ap.add_argument(
            f"--{field.name.replace('_', '-')}",
            type=argparse_type,
            metavar=metavar,
            required=field.default is MISSING and field.default_factory is MISSING,  # type: ignore
            help=help,
        )

    def __init__(
        self,
        full_argparse_namespace,
        create: bool,
        collab_name: str,
        enable: t.Optional[int],
        is_json: bool,
    ) -> None:
        self.namespace = full_argparse_namespace
        self.create = create
        self.edit_kwargs = {}
        self.collab_name = collab_name
        if is_json:
            self.edit_kwargs = json.loads(collab_name)
            self.collab_name = self.edit_kwargs["name"]

        # Technically you could combine the flags and JSON, but you'd be weird
        if create:
            self.edit_kwargs["name"] = collab_name
            self.edit_kwargs["enabled"] = True
            self.edit_kwargs["api"] = self._API_CLS.get_name()

        if enable is not None:
            self.edit_kwargs["enabled"] = bool(enable)

        for field in fields(self._API_CLS.get_config_class()):
            if not field.init:
                if field.name == "api":
                    self.edit_kwargs.pop("api")
                continue
            if field.name in self._IGNORE_FIELDS:
                continue
            val = getattr(full_argparse_namespace, field.name)
            if val is not None:
                self.edit_kwargs[field.name] = val

    def execute(self, settings: CLISettings) -> None:
        existing = settings.get_collab(self.collab_name)

        if existing:
            if self.create:
                raise CommandError(
                    f'there\'s an existing collaboration named "{self.collab_name}"', 2
                )
            if existing.api != self._API_CLS.get_name():
                raise CommandError(
                    f"the existing collab is for the {existing.api} api, delete that one first",
                    2,
                )
            assert (
                existing.__class__ == self._API_CLS.get_config_class()
            ), "api name the same, but class different?"
            for name, val in self.edit_kwargs.items():
                setattr(existing, name, val)
            settings._state.update_collab(existing)
        elif self.create:
            logging.debug("Creating config with args: %s", self.edit_kwargs)
            to_create = self._API_CLS.get_config_class()(**self.edit_kwargs)
            settings._state.update_collab(to_create)
        else:
            raise CommandError("no such config! Did you mean to use --create?", 2)


class ConfigCollabForAPICommand(command_base.CommandWithSubcommands):
    """Create and edit collaborations for APIs"""

    @classmethod
    def get_name(cls) -> str:
        return "edit"

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        cls._SUBCOMMANDS = [
            cls._create_command_for_api(api)
            for api in settings.apis
            if api.__class__ is not StaticSampleSignalExchangeAPI
        ]

    @classmethod
    def _create_command_for_api(
        cls, api: SignalExchangeAPI
    ) -> t.Type[command_base.Command]:
        """Don't try this at home!"""

        class _GeneratedUpdateCommand(_UpdateCollabCommand):
            _API_CLS = api.__class__

        _GeneratedUpdateCommand.__name__ = (
            f"{_GeneratedUpdateCommand.__name__}_{api.get_name()}"
        )

        return _GeneratedUpdateCommand


class ConfigCollabDeleteCommand(command_base.Command):
    """Delete collaborations"""

    @classmethod
    def get_name(cls) -> str:
        return "delete"

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        ap.add_argument("collab_name", help="the collab to delete")

    def __init__(self, collab_name: str) -> None:
        self.collab_name = collab_name

    def execute(self, settings: CLISettings) -> None:
        collab = settings.get_collab(self.collab_name)
        if collab is None:
            raise CommandError("No such collab", 2)
        settings._state.delete_collab(collab)  # TODO clean private member access


class ConfigCollabCommand(command_base.CommandWithSubcommands):
    """Configure collaborations"""

    _SUBCOMMANDS = [
        ConfigCollabListCommand,
        ConfigCollabForAPICommand,
        ConfigCollabDeleteCommand,
    ]

    @classmethod
    def get_name(cls) -> str:
        return "collab"

    def execute(self, settings: CLISettings) -> None:
        ConfigCollabListCommand().execute(settings)


class ConfigExtensionsCommand(command_base.Command):
    """Configure extensions"""

    @classmethod
    def get_name(cls) -> str:
        return "extensions"

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        ap.add_argument(
            "action",
            choices=["list", "add", "remove"],
            default="list",
            help="what to do",
        )
        ap.add_argument(
            "module",
            nargs="?",
            help="the module path to the extension. foo.bar.baz",
        )
        ap.set_defaults(is_config=True)

    def __init__(self, action: str, module: t.Optional[str]) -> None:
        self.action = {
            "list": self.execute_list,
            "add": self.execute_add,
            "remove": self.execute_remove,
        }[action]
        self.module = module

    def execute(self, settings: CLISettings) -> None:
        self.action(settings)

    def execute_list(self, settings: CLISettings) -> None:
        if self.module:
            manifest = self.get_manifest(self.module)
            self.print_extension(manifest)
            return
        for module_name in sorted(settings.get_persistent_config().extensions):
            print(module_name)
            manifest = self.get_manifest(module_name)
            self.print_extension(manifest, indent=2)

    def get_manifest(self, module_name: str) -> ThreatExchangeExtensionManifest:
        try:
            return ThreatExchangeExtensionManifest.load_from_module_name(module_name)
        except ValueError as ve:
            raise CommandError(str(ve), 2)

    def execute_add(self, settings: CLISettings) -> None:
        if not self.module:
            raise CommandError("module is required", 2)

        manifest = self.get_manifest(self.module)

        # Validate our new setups by pretending to create a new mapping with the new classes
        content_and_settings = tx_meta.SignalTypeMapping(
            list(
                itertools.chain(
                    settings.get_all_content_types(), manifest.content_types
                )
            ),
            list(
                itertools.chain(settings.get_all_signal_types(), manifest.signal_types)
            ),
        )

        # For APIs, we also need to make sure they can be instanciated without args for the CLI
        apis = []
        for new_api in manifest.apis:
            try:
                instance = new_api()
            except Exception as e:
                logging.exception(f"Failed to instanciante API {new_api.get_name()}")
                raise CommandError(
                    f"Not able to instanciate API {new_api.get_name()} - throws {e}"
                )
            apis.append(instance)
        apis.extend(settings.apis.get_all())
        tx_meta.FetcherMapping(apis)

        self.print_extension(manifest)

        config = settings.get_persistent_config()
        config.extensions.add(self.module)
        settings.set_persistent_config(config)

    def execute_remove(self, settings: CLISettings) -> None:
        if not self.module:
            raise CommandError("Which module you are remove is required", 2)
        config = settings.get_persistent_config()
        if self.module not in config.extensions:
            raise CommandError(f"You haven't added {self.module}", 2)
        config.extensions.remove(self.module)
        settings.set_persistent_config(config)

    def print_extension(
        self, manifest: ThreatExchangeExtensionManifest, indent=0
    ) -> None:
        space = " " * indent
        level2 = f"\n{space}  "
        if manifest.signal_types:
            print(f"{space}Signal:{level2}", end="")
            print(
                level2.join(
                    f"{s.get_name()} - {s.__name__}" for s in manifest.signal_types
                )
            )
        if manifest.content_types:
            print(f"{space}Content:{level2}", end="")
            print(
                level2.join(
                    f"{c.get_name()} - {c.__name__}" for c in manifest.content_types
                )
            )
        if manifest.apis:
            print(f"{space}Content:{level2}", end="")
            print(level2.join(f"{a.get_name()} - {a.__name__}" for a in manifest.apis))


class ConfigSignalCommand(command_base.Command):
    """Configure and view available SignalTypes"""

    @classmethod
    def get_name(cls) -> str:
        return "signal"

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        ap.add_argument(
            "action",
            choices=["list"],
            nargs="?",
            default="list",
            help="what to do",
        )

    def __init__(self, action: str) -> None:
        self.action = {
            "list": self.execute_list,
        }[action]

    def execute(self, settings: CLISettings) -> None:
        self.action(settings)

    def execute_list(self, settings: CLISettings) -> None:
        signals = settings.get_all_signal_types(default_to_sample=False)
        for name, class_name in sorted(
            (st.get_name(), _fully_qualified_name(st)) for st in signals
        ):
            print(name, class_name)


class ConfigContentCommand(command_base.Command):
    """Configure and view available ContentTypes"""

    @classmethod
    def get_name(cls) -> str:
        return "content"

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        ap.add_argument(
            "action",
            choices=["list"],
            nargs="?",
            default="list",
            help="what to do",
        )

    def __init__(self, action: str) -> None:
        self.action = {
            "list": self.execute_list,
        }[action]

    def execute(self, settings: CLISettings) -> None:
        self.action(settings)

    def execute_list(self, settings: CLISettings) -> None:
        content_types = settings.get_all_content_types()
        for name, class_name in sorted(
            (c.get_name(), _fully_qualified_name(c)) for c in content_types
        ):
            print(name, class_name)


class ConfigThreatExchangeAPICommand(command_base.Command):
    """Configure Facebook ThreatExchange integration"""

    @classmethod
    def get_name(cls) -> str:
        return FBThreatExchangeSignalExchangeAPI.get_name()

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        import_cmds = ap.add_mutually_exclusive_group()
        import_cmds.add_argument(
            "--list-available-collabs",
            "-L",
            action="store_true",
            help="query the API to list available collabs",
        )
        import_cmds.add_argument(
            "--import-collab",
            "-I",
            type=int,
            help="import a collaboration by privacy group ID",
        )

        # Not actually a type of import cmd, but to add exclusivity logic
        config_cmds = import_cmds.add_argument_group()
        config_cmds.add_argument(
            "--api-token",
            help="set the default api token (https://developers.facebook.com/tools/accesstoken/)",
        )

    def __init__(
        self,
        api_token: t.Optional[str],
        list_available_collabs: bool,
        import_collab: t.Optional[int],
    ) -> None:
        self.api_token = api_token
        self.action = self.execute_config
        if list_available_collabs:
            self.action = self.execute_list_collabs
        elif import_collab is not None:
            tmp_for_typing = import_collab
            self.action = lambda s: self.execute_import(s, tmp_for_typing)

    def execute(self, settings: CLISettings) -> None:
        self.action(settings)

    def get_te_api(self, settings: CLISettings) -> ThreatExchangeAPI:
        te = next(
            (
                api
                for api in settings.apis
                if isinstance(api, FBThreatExchangeSignalExchangeAPI)
            ),
            None,
        )
        assert te is not None
        return te.api

    def execute_list_collabs(self, settings: CLISettings) -> None:
        api = self.get_te_api(settings)

        unique_privacy_groups = {
            pg.id: pg for pg in api.get_threat_privacy_groups_member()
        }
        unique_privacy_groups.update(
            (pg.id, pg) for pg in api.get_threat_privacy_groups_owner()
        )

        max_width = os.get_terminal_size().columns

        for pg in sorted(unique_privacy_groups.values(), key=lambda pg: pg.name):
            if not pg.threat_updates_enabled:
                continue
            line = f"{pg.id} {pg.name} - {pg.description}".replace("\n", " ")
            if len(line) > max_width:
                line = f"{line[:max_width-3]}..."
            print(line)

    def execute_import(self, settings: CLISettings, privacy_group_id: int) -> None:
        api = self.get_te_api(settings)
        pg = api.get_privacy_group(privacy_group_id)
        if settings.get_collab(pg.name) is not None:
            raise CommandError(
                f"A collaboration already exists with the name {pg.name}", 2
            )
        settings._state.update_collab(
            FBThreatExchangeCollabConfig(name=pg.name, privacy_group=privacy_group_id)
        )

    def execute_config(self, settings: CLISettings) -> None:
        if self.api_token is not None:
            config = settings.get_persistent_config()
            config.fb_threatexchange_api_token = self.api_token
            settings.set_persistent_config(config)


class ConfigNCMECAPICommand(command_base.Command):
    """Configure NCMEC hash api integration"""

    @classmethod
    def get_name(cls) -> str:
        return NCMECSignalExchangeAPI.get_name()

    @classmethod
    def init_argparse(cls, settings: CLISettings, ap: argparse.ArgumentParser) -> None:
        ap.add_argument(
            "--credentials",
            metavar="STR",
            nargs=2,
            help="set the username and password to access the NCMEC API",
        )

    def __init__(
        self,
        credentials: t.List[str],
    ) -> None:
        self.credentials = (credentials[0], credentials[1]) if credentials else None

    def execute(self, settings: CLISettings) -> None:
        if self.credentials is not None:
            config = settings.get_persistent_config()
            config.ncmec_credentials = self.credentials
            settings.set_persistent_config(config)


class ConfigAPICommand(command_base.CommandWithSubcommands):
    """Configure and view available SignalExchangeAPIs"""

    _SUBCOMMANDS = [ConfigThreatExchangeAPICommand, ConfigNCMECAPICommand]

    @classmethod
    def get_name(cls) -> str:
        return "api"

    def execute(self, settings: CLISettings) -> None:
        apis = settings.apis.get_all()
        for name in sorted(a.get_name() for a in apis):
            print(name)


class ConfigCommand(command_base.CommandWithSubcommands):
    """Configure the CLI"""

    _SUBCOMMANDS = [
        ConfigCollabCommand,
        ConfigSignalCommand,
        ConfigContentCommand,
        ConfigAPICommand,
        ConfigExtensionsCommand,
    ]


def _fully_qualified_name(klass: t.Type):
    return f"{klass.__module__}.{klass.__qualname__}"
