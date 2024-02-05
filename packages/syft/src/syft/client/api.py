# future
from __future__ import annotations

# stdlib
from collections import OrderedDict
import inspect
from inspect import Parameter
from inspect import signature
import types
from typing import Any
from typing import Callable
from typing import Dict
from typing import List
from typing import Optional
from typing import TYPE_CHECKING
from typing import Tuple
from typing import Union
from typing import _GenericAlias
from typing import get_args
from typing import get_origin

# third party
from nacl.exceptions import BadSignatureError
from pydantic import EmailStr
from result import OkErr
from result import Result
from typeguard import check_type

# relative
from ..abstract_node import AbstractNode
from ..node.credentials import SyftSigningKey
from ..node.credentials import SyftVerifyKey
from ..protocol.data_protocol import PROTOCOL_TYPE
from ..protocol.data_protocol import get_data_protocol
from ..protocol.data_protocol import migrate_args_and_kwargs
from ..serde.deserialize import _deserialize
from ..serde.recursive import index_syft_by_module_name
from ..serde.serializable import serializable
from ..serde.serialize import _serialize
from ..serde.signature import Signature
from ..serde.signature import signature_remove_context
from ..serde.signature import signature_remove_self
from ..service.context import AuthedServiceContext
from ..service.context import ChangeContext
from ..service.response import SyftAttributeError
from ..service.response import SyftError
from ..service.response import SyftSuccess
from ..service.service import UserLibConfigRegistry
from ..service.service import UserServiceConfigRegistry
from ..service.user.user_roles import ServiceRole
from ..service.warnings import APIEndpointWarning
from ..service.warnings import WarningContext
from ..types.identity import Identity
from ..types.syft_object import SYFT_OBJECT_VERSION_1
from ..types.syft_object import SyftBaseObject
from ..types.syft_object import SyftMigrationRegistry
from ..types.syft_object import SyftObject
from ..types.uid import LineageID
from ..types.uid import UID
from ..util.autoreload import autoreload_enabled
from ..util.telemetry import instrument
from .connection import NodeConnection

if TYPE_CHECKING:
    # relative
    from ..service.job.job_stash import Job


class APIRegistry:
    __api_registry__: Dict[Tuple, SyftAPI] = OrderedDict()

    @classmethod
    def set_api_for(
        cls,
        node_uid: Union[UID, str],
        user_verify_key: Union[SyftVerifyKey, str],
        api: SyftAPI,
    ) -> None:
        if isinstance(node_uid, str):
            node_uid = UID.from_string(node_uid)

        if isinstance(user_verify_key, str):
            user_verify_key = SyftVerifyKey.from_string(user_verify_key)

        key = (node_uid, user_verify_key)

        cls.__api_registry__[key] = api

    @classmethod
    def api_for(cls, node_uid: UID, user_verify_key: SyftVerifyKey) -> SyftAPI:
        key = (node_uid, user_verify_key)
        return cls.__api_registry__.get(key, None)

    @classmethod
    def get_all_api(cls) -> List[SyftAPI]:
        return list(cls.__api_registry__.values())

    @classmethod
    def get_by_recent_node_uid(cls, node_uid: UID) -> Optional[SyftAPI]:
        for key, api in reversed(cls.__api_registry__.items()):
            if key[0] == node_uid:
                return api
        return None


@serializable()
class APIEndpoint(SyftObject):
    __canonical_name__ = "APIEndpoint"
    __version__ = SYFT_OBJECT_VERSION_1

    id: UID
    service_path: str
    module_path: str
    name: str
    description: str
    doc_string: Optional[str]
    signature: Signature
    has_self: bool = False
    pre_kwargs: Optional[Dict[str, Any]]
    warning: Optional[APIEndpointWarning]


@serializable()
class LibEndpoint(SyftBaseObject):
    __canonical_name__ = "LibEndpoint"
    __version__ = SYFT_OBJECT_VERSION_1

    # TODO: bad name, change
    service_path: str
    module_path: str
    name: str
    description: str
    doc_string: Optional[str]
    signature: Signature
    has_self: bool = False
    pre_kwargs: Optional[Dict[str, Any]]


@serializable(attrs=["signature", "credentials", "serialized_message"])
class SignedSyftAPICall(SyftObject):
    __canonical_name__ = "SignedSyftAPICall"
    __version__ = SYFT_OBJECT_VERSION_1

    credentials: SyftVerifyKey
    signature: bytes
    serialized_message: bytes
    cached_deseralized_message: Optional[SyftAPICall] = None

    @property
    def message(self) -> SyftAPICall:
        # from deserialize we might not have this attr because __init__ is skipped
        if not hasattr(self, "cached_deseralized_message"):
            self.cached_deseralized_message = None

        if self.cached_deseralized_message is None:
            self.cached_deseralized_message = _deserialize(
                blob=self.serialized_message, from_bytes=True
            )

        return self.cached_deseralized_message

    @property
    def is_valid(self) -> Result[SyftSuccess, SyftError]:
        try:
            _ = self.credentials.verify_key.verify(
                self.serialized_message, self.signature
            )
        except BadSignatureError:
            return SyftError(message="BadSignatureError")

        return SyftSuccess(message="Credentials are valid")


@instrument
@serializable()
class SyftAPICall(SyftObject):
    # version
    __canonical_name__ = "SyftAPICall"
    __version__ = SYFT_OBJECT_VERSION_1

    # fields
    node_uid: UID
    path: str
    args: List
    kwargs: Dict[str, Any]
    blocking: bool = True

    def sign(self, credentials: SyftSigningKey) -> SignedSyftAPICall:
        signed_message = credentials.signing_key.sign(_serialize(self, to_bytes=True))

        return SignedSyftAPICall(
            credentials=credentials.verify_key,
            serialized_message=signed_message.message,
            signature=signed_message.signature,
        )


@instrument
@serializable()
class SyftAPIData(SyftBaseObject):
    # version
    __canonical_name__ = "SyftAPIData"
    __version__ = SYFT_OBJECT_VERSION_1

    # fields
    data: Any

    def sign(self, credentials: SyftSigningKey) -> SignedSyftAPICall:
        signed_message = credentials.signing_key.sign(_serialize(self, to_bytes=True))

        return SignedSyftAPICall(
            credentials=credentials.verify_key,
            serialized_message=signed_message.message,
            signature=signed_message.signature,
        )


class RemoteFunction(SyftObject):
    __canonical_name__ = "RemoteFunction"
    __version__ = SYFT_OBJECT_VERSION_1
    __repr_attrs__ = [
        "id",
        "node_uid",
        "signature",
        "path",
    ]

    node_uid: UID
    signature: Signature
    path: str
    make_call: Callable
    pre_kwargs: Optional[Dict[str, Any]]
    communication_protocol: PROTOCOL_TYPE
    warning: Optional[APIEndpointWarning]

    @property
    def __ipython_inspector_signature_override__(self) -> Optional[Signature]:
        return self.signature

    def prepare_args_and_kwargs(
        self, args: List[Any], kwargs: Dict[str, Any]
    ) -> Union[SyftError, Tuple[List[Any], Dict[str, Any]]]:
        # Validate and migrate args and kwargs
        res = validate_callable_args_and_kwargs(args, kwargs, self.signature)
        if isinstance(res, SyftError):
            return res
        args, kwargs = res

        args, kwargs = migrate_args_and_kwargs(
            to_protocol=self.communication_protocol, args=args, kwargs=kwargs
        )

        return args, kwargs

    def __call__(self, *args: Tuple[Any, ...], **kwargs: Dict[str, Any]) -> Any:
        if "blocking" in self.signature.parameters:
            raise Exception(
                f"Signature {self.signature} can't have 'blocking' kwarg because it's reserved"
            )

        blocking = True
        if "blocking" in kwargs:
            blocking = bool(kwargs["blocking"])
            del kwargs["blocking"]

        res = self.prepare_args_and_kwargs(args, kwargs)
        if isinstance(res, SyftError):
            return res

        _valid_args, _valid_kwargs = res
        if self.pre_kwargs:
            _valid_kwargs.update(self.pre_kwargs)

        _valid_kwargs["communication_protocol"] = self.communication_protocol

        api_call = SyftAPICall(
            node_uid=self.node_uid,
            path=self.path,
            args=_valid_args,
            kwargs=_valid_kwargs,
            blocking=blocking,
        )

        allowed = self.warning.show() if self.warning else True
        if not allowed:
            return
        result = self.make_call(api_call=api_call)

        result, _ = migrate_args_and_kwargs(
            [result], kwargs={}, to_latest_protocol=True
        )
        result = result[0]
        return result


class RemoteUserCodeFunction(RemoteFunction):
    __canonical_name__ = "RemoteUserFunction"
    __version__ = SYFT_OBJECT_VERSION_1
    __repr_attrs__ = RemoteFunction.__repr_attrs__ + ["user_code_id"]

    api: SyftAPI

    def prepare_args_and_kwargs(
        self, args: List[Any], kwargs: Dict[str, Any]
    ) -> Union[SyftError, Tuple[List[Any], Dict[str, Any]]]:
        # relative
        from ..service.action.action_object import convert_to_pointers

        # Validate and migrate args and kwargs
        res = validate_callable_args_and_kwargs(args, kwargs, self.signature)
        if isinstance(res, SyftError):
            return res
        args, kwargs = res

        args, kwargs = convert_to_pointers(
            api=self.api,
            node_uid=self.node_uid,
            args=args,
            kwargs=kwargs,
        )

        args, kwargs = migrate_args_and_kwargs(
            to_protocol=self.communication_protocol, args=args, kwargs=kwargs
        )

        return args, kwargs

    @property
    def user_code_id(self) -> Optional[UID]:
        if self.pre_kwargs:
            return self.pre_kwargs.get("uid", None)
        else:
            return None

    @property
    def jobs(self) -> Union[List[Job], SyftError]:
        if self.user_code_id is None:
            return SyftError(message="Could not find user_code_id")
        api_call = SyftAPICall(
            node_uid=self.node_uid,
            path="job.get_by_user_code_id",
            args=[self.user_code_id],
            kwargs={},
            blocking=True,
        )
        return self.make_call(api_call=api_call)


def generate_remote_function(
    api: SyftAPI,
    node_uid: UID,
    signature: Signature,
    path: str,
    make_call: Callable,
    pre_kwargs: Optional[Dict[str, Any]],
    communication_protocol: PROTOCOL_TYPE,
    warning: Optional[APIEndpointWarning],
) -> RemoteFunction:
    if "blocking" in signature.parameters:
        raise Exception(
            f"Signature {signature} can't have 'blocking' kwarg because it's reserved"
        )

    # UserCodes are always code.call with a user_code_id
    if path == "code.call" and pre_kwargs is not None and "uid" in pre_kwargs:
        remote_function = RemoteUserCodeFunction(
            api=api,
            node_uid=node_uid,
            signature=signature,
            path=path,
            make_call=make_call,
            pre_kwargs=pre_kwargs,
            communication_protocol=communication_protocol,
            warning=warning,
            user_code_id=pre_kwargs["uid"],
        )
    else:
        remote_function = RemoteFunction(
            node_uid=node_uid,
            signature=signature,
            path=path,
            make_call=make_call,
            pre_kwargs=pre_kwargs,
            communication_protocol=communication_protocol,
            warning=warning,
        )

    return remote_function


def generate_remote_lib_function(
    api: SyftAPI,
    node_uid: UID,
    signature: Signature,
    path: str,
    module_path: str,
    make_call: Callable,
    communication_protocol: PROTOCOL_TYPE,
    pre_kwargs: Dict[str, Any],
) -> Any:
    if "blocking" in signature.parameters:
        raise Exception(
            f"Signature {signature} can't have 'blocking' kwarg because its reserved"
        )

    def wrapper(
        *args: Tuple[Any, ...], **kwargs: Dict[str, Any]
    ) -> Union[SyftError, Any]:
        # relative
        from ..service.action.action_object import TraceResult

        if TraceResult._client is not None:
            wrapper_make_call = TraceResult._client.api.make_call
            wrapper_node_uid = TraceResult._client.api.node_uid
        else:
            # somehow this is necessary to prevent shadowing problems
            wrapper_make_call = make_call
            wrapper_node_uid = node_uid
        blocking = True
        if "blocking" in kwargs:
            blocking = bool(kwargs["blocking"])
            del kwargs["blocking"]

        res = validate_callable_args_and_kwargs(args, kwargs, signature)

        if isinstance(res, SyftError):
            return res
        _valid_args, _valid_kwargs = res

        if pre_kwargs:
            _valid_kwargs.update(pre_kwargs)

        # relative
        from ..service.action.action_object import Action
        from ..service.action.action_object import ActionType
        from ..service.action.action_object import convert_to_pointers

        action_args, action_kwargs = convert_to_pointers(
            api, wrapper_node_uid, _valid_args, _valid_kwargs
        )

        # e.g. numpy.array -> numpy, array
        module, op = module_path.rsplit(".", 1)
        action = Action(
            path=module,
            op=op,
            remote_self=None,
            args=[x.syft_lineage_id for x in action_args],
            kwargs={k: v.syft_lineage_id for k, v in action_kwargs},
            action_type=ActionType.FUNCTION,
            # TODO: fix
            result_id=LineageID(UID(), 1),
        )
        service_args = [action]
        # TODO: implement properly
        TraceResult.result += [action]

        api_call = SyftAPICall(
            node_uid=wrapper_node_uid,
            path=path,
            args=service_args,
            kwargs={},
            blocking=blocking,
        )

        result = wrapper_make_call(api_call=api_call)
        return result

    wrapper.__ipython_inspector_signature_override__ = signature
    return wrapper


@serializable()
class APIModule:
    _modules: List[str]
    path: str

    def __init__(self, path: str) -> None:
        self._modules = []
        self.path = path

    def _add_submodule(
        self, attr_name: str, module_or_func: Union[Callable, APIModule]
    ) -> None:
        setattr(self, attr_name, module_or_func)
        self._modules.append(attr_name)

    def __getattribute__(self, name: str) -> Any:
        try:
            return object.__getattribute__(self, name)
        except AttributeError:
            raise SyftAttributeError(
                f"'APIModule' api{self.path} object has no submodule or method '{name}', "
                "you may not have permission to access the module you are trying to access"
            )

    def __getitem__(self, key: Union[str, int]) -> Any:
        if hasattr(self, "get_all"):
            return self.get_all()[key]
        raise NotImplementedError

    def _repr_html_(self) -> Any:
        if not hasattr(self, "get_all"):
            return NotImplementedError
        results = self.get_all()
        return results._repr_html_()


def debox_signed_syftapicall_response(
    signed_result: SignedSyftAPICall,
) -> Union[Any, SyftError]:
    if not isinstance(signed_result, SignedSyftAPICall):
        return SyftError(message="The result is not signed")  # type: ignore

    if not signed_result.is_valid:
        return SyftError(message="The result signature is invalid")
    return signed_result.message.data


def downgrade_signature(signature: Signature, object_versions: Dict) -> Signature:
    migrated_parameters = []
    for _, parameter in signature.parameters.items():
        annotation = unwrap_and_migrate_annotation(
            parameter.annotation, object_versions
        )
        migrated_parameter = Parameter(
            name=parameter.name,
            default=parameter.default,
            annotation=annotation,
            kind=parameter.kind,
        )
        migrated_parameters.append(migrated_parameter)

    migrated_return_annotation = unwrap_and_migrate_annotation(
        signature.return_annotation, object_versions
    )

    try:
        new_signature = Signature(
            parameters=migrated_parameters,
            return_annotation=migrated_return_annotation,
        )
    except Exception as e:
        raise e

    return new_signature


def unwrap_and_migrate_annotation(annotation: Any, object_versions: Dict) -> Any:
    args = get_args(annotation)
    origin = get_origin(annotation)
    if len(args) == 0:
        if (
            isinstance(annotation, type)
            and issubclass(annotation, SyftBaseObject)
            and annotation.__canonical_name__ in object_versions
        ):
            downgrade_to_version = int(
                max(object_versions[annotation.__canonical_name__])
            )
            downgrade_klass_name = SyftMigrationRegistry.__migration_version_registry__[
                annotation.__canonical_name__
            ][downgrade_to_version]
            new_arg = index_syft_by_module_name(downgrade_klass_name)
            return new_arg
        else:
            return annotation

    migrated_annotations = []
    for arg in args:
        migrated_annotation = unwrap_and_migrate_annotation(arg, object_versions)
        migrated_annotations.append(migrated_annotation)

    migrated_annotations_tuple = tuple(migrated_annotations)

    if hasattr(annotation, "copy_with"):
        return annotation.copy_with(migrated_annotations_tuple)
    elif origin is not None:
        return origin[migrated_annotations_tuple]
    else:
        return migrated_annotation[0]


@instrument
@serializable(
    attrs=[
        "endpoints",
        "node_uid",
        "node_name",
        "lib_endpoints",
        "communication_protocol",
    ]
)
class SyftAPI(SyftObject):
    # version
    __canonical_name__ = "SyftAPI"
    __version__ = SYFT_OBJECT_VERSION_1

    # fields
    connection: Optional[NodeConnection] = None
    node_uid: Optional[UID] = None
    node_name: Optional[str] = None
    endpoints: Dict[str, APIEndpoint]
    lib_endpoints: Optional[Dict[str, LibEndpoint]] = None
    api_module: Optional[APIModule] = None
    libs: Optional[APIModule] = None
    signing_key: Optional[SyftSigningKey] = None
    # serde / storage rules
    refresh_api_callback: Optional[Callable] = None
    __user_role: ServiceRole = ServiceRole.NONE
    communication_protocol: PROTOCOL_TYPE

    # def __post_init__(self) -> None:
    #     pass

    @staticmethod
    def for_user(
        node: AbstractNode,
        communication_protocol: PROTOCOL_TYPE,
        user_verify_key: Optional[SyftVerifyKey] = None,
    ) -> SyftAPI:
        # relative
        # TODO: Maybe there is a possibility of merging ServiceConfig and APIEndpoint
        from ..service.code.user_code_service import UserCodeService

        # find user role by verify_key
        # TODO: we should probably not allow empty verify keys but instead make user always register
        role = node.get_role_for_credentials(user_verify_key)
        _user_service_config_registry = UserServiceConfigRegistry.from_role(role)
        _user_lib_config_registry = UserLibConfigRegistry.from_user(user_verify_key)
        endpoints: Dict[str, APIEndpoint] = {}
        lib_endpoints: Dict[str, LibEndpoint] = {}
        warning_context = WarningContext(
            node=node, role=role, credentials=user_verify_key
        )

        # If server uses a higher protocol version than client, then
        # signatures needs to be downgraded.
        if node.current_protocol == "dev" and communication_protocol != "dev":
            # We assume dev is the highest staged protocol
            signature_needs_downgrade = True
        else:
            signature_needs_downgrade = node.current_protocol != "dev" and int(
                node.current_protocol
            ) > int(communication_protocol)
        data_protocol = get_data_protocol()

        if signature_needs_downgrade:
            object_version_for_protocol = data_protocol.get_object_versions(
                communication_protocol
            )

        for (
            path,
            service_config,
        ) in _user_service_config_registry.get_registered_configs().items():
            if not service_config.is_from_lib:
                service_warning = service_config.warning
                if service_warning:
                    service_warning = service_warning.message_from(warning_context)
                    service_warning.enabled = node.enable_warnings

                signature = (
                    downgrade_signature(
                        signature=service_config.signature,
                        object_versions=object_version_for_protocol,
                    )
                    if signature_needs_downgrade
                    else service_config.signature
                )

                endpoint = APIEndpoint(
                    service_path=path,
                    module_path=path,
                    name=service_config.public_name,
                    description="",
                    doc_string=service_config.doc_string,
                    signature=signature,  # TODO: Migrate signature based on communication protocol
                    has_self=False,
                    warning=service_warning,
                )
                endpoints[path] = endpoint

        for (
            path,
            lib_config,
        ) in _user_lib_config_registry.get_registered_configs().items():
            endpoint = LibEndpoint(
                service_path="action.execute",
                module_path=path,
                name=lib_config.public_name,
                description="",
                doc_string=lib_config.doc_string,
                signature=lib_config.signature,
                has_self=False,
            )
            lib_endpoints[path] = endpoint

        # 🟡 TODO 35: fix root context
        context = AuthedServiceContext(node=node, credentials=user_verify_key)
        method = node.get_method_with_context(UserCodeService.get_all_for_user, context)
        code_items = method()

        for code_item in code_items:
            path = "code.call"
            unique_path = f"code.call_{code_item.service_func_name}"
            endpoint = APIEndpoint(
                service_path=path,
                module_path=path,
                name=code_item.service_func_name,
                description="",
                doc_string=f"Users custom func {code_item.service_func_name}",
                signature=code_item.signature,
                has_self=False,
                pre_kwargs={"uid": code_item.id},
            )
            endpoints[unique_path] = endpoint

        return SyftAPI(
            node_name=node.name,
            node_uid=node.id,
            endpoints=endpoints,
            lib_endpoints=lib_endpoints,
            __user_role=role,
            communication_protocol=communication_protocol,
        )

    @property
    def user_role(self) -> ServiceRole:
        return self.__user_role

    def make_call(self, api_call: SyftAPICall) -> Result:
        signed_call = api_call.sign(credentials=self.signing_key)
        if self.connection is not None:
            signed_result = self.connection.make_call(signed_call)
        else:
            return SyftError(message="API connection is None")

        result = debox_signed_syftapicall_response(signed_result=signed_result)

        if isinstance(result, OkErr):
            if result.is_ok():
                res = result.ok()
                # we update the api when we create objects that change it
                self.update_api(res)
                return res
            else:
                return result.err()
        return result

    def update_api(self, api_call_result: Any) -> None:
        # TODO: hacky stuff with typing and imports to prevent circular imports
        # relative
        from ..service.request.request import Request
        from ..service.request.request import UserCodeStatusChange

        if isinstance(api_call_result, Request) and any(
            isinstance(x, UserCodeStatusChange) for x in api_call_result.changes
        ):
            if self.refresh_api_callback is not None:
                self.refresh_api_callback()

    @staticmethod
    def _add_route(
        api_module: APIModule, endpoint: APIEndpoint, endpoint_method: Callable
    ) -> None:
        """Recursively create a module path to the route endpoint."""

        _modules = endpoint.module_path.split(".")[:-1] + [endpoint.name]

        _self = api_module
        _last_module = _modules.pop()
        while _modules:
            module = _modules.pop(0)
            if not hasattr(_self, module):
                submodule_path = f"{_self.path}.{module}"
                _self._add_submodule(module, APIModule(path=submodule_path))
            _self = getattr(_self, module)
        _self._add_submodule(_last_module, endpoint_method)

    def generate_endpoints(self) -> None:
        def build_endpoint_tree(
            endpoints: Dict[str, LibEndpoint], communication_protocol: PROTOCOL_TYPE
        ) -> APIModule:
            api_module = APIModule(path="")
            for _, v in endpoints.items():
                signature = v.signature
                if not v.has_self:
                    signature = signature_remove_self(signature)
                signature = signature_remove_context(signature)
                if isinstance(v, APIEndpoint):
                    endpoint_function = generate_remote_function(
                        self,
                        self.node_uid,
                        signature,
                        v.service_path,
                        self.make_call,
                        pre_kwargs=v.pre_kwargs,
                        warning=v.warning,
                        communication_protocol=communication_protocol,
                    )
                elif isinstance(v, LibEndpoint):
                    endpoint_function = generate_remote_lib_function(
                        self,
                        self.node_uid,
                        signature,
                        v.service_path,
                        v.module_path,
                        self.make_call,
                        pre_kwargs=v.pre_kwargs,
                        communication_protocol=communication_protocol,
                    )

                endpoint_function.__doc__ = v.doc_string
                self._add_route(api_module, v, endpoint_function)
            return api_module

        if self.lib_endpoints is not None:
            self.libs = build_endpoint_tree(
                self.lib_endpoints, self.communication_protocol
            )
        self.api_module = build_endpoint_tree(
            self.endpoints, self.communication_protocol
        )

    @property
    def services(self) -> Optional[APIModule]:
        if self.api_module is None:
            self.generate_endpoints()
        return self.api_module

    @property
    def lib(self) -> Optional[APIModule]:
        if self.libs is None:
            self.generate_endpoints()
        return self.libs

    def has_service(self, service_name: str) -> bool:
        return hasattr(self.services, service_name)

    def has_lib(self, lib_name: str) -> bool:
        return hasattr(self.lib, lib_name)

    def __repr__(self) -> str:
        modules = self.services
        _repr_str = "client.api.services\n"
        if modules is not None:
            for attr_name in modules._modules:
                module_or_func = getattr(modules, attr_name)
                module_path_str = f"client.api.services.{attr_name}"
                _repr_str += f"\n{module_path_str}\n\n"
                if hasattr(module_or_func, "_modules"):
                    for func_name in module_or_func._modules:
                        func = getattr(module_or_func, func_name)
                        sig = func.__ipython_inspector_signature_override__
                        _repr_str += f"{module_path_str}.{func_name}{sig}\n\n"
        return _repr_str


# code from here:
# https://github.com/ipython/ipython/blob/339c0d510a1f3cb2158dd8c6e7f4ac89aa4c89d8/IPython/core/oinspect.py#L370
def _render_signature(obj_signature: Signature, obj_name: str) -> str:
    """
    This was mostly taken from inspect.Signature.__str__.
    Look there for the comments.
    The only change is to add linebreaks when this gets too long.
    """
    result = []
    pos_only = False
    kw_only = True
    for param in obj_signature.parameters.values():
        if param.kind == inspect._POSITIONAL_ONLY:
            pos_only = True
        elif pos_only:
            result.append("/")
            pos_only = False

        if param.kind == inspect._VAR_POSITIONAL:
            kw_only = False
        elif param.kind == inspect._KEYWORD_ONLY and kw_only:
            result.append("*")
            kw_only = False

        result.append(str(param))

    if pos_only:
        result.append("/")

    # add up name, parameters, braces (2), and commas
    if len(obj_name) + sum(len(r) + 2 for r in result) > 75:
        # This doesn’t fit behind “Signature: ” in an inspect window.
        rendered = "{}(\n{})".format(obj_name, "".join(f"    {r},\n" for r in result))
    else:
        rendered = "{}({})".format(obj_name, ", ".join(result))

    if obj_signature.return_annotation is not inspect._empty:
        anno = inspect.formatannotation(obj_signature.return_annotation)
        rendered += f" -> {anno}"

    return rendered


def _getdef(self: Any, obj: Any, oname: str = "") -> Union[str, None]:
    """Return the call signature for any callable object.
    If any exception is generated, None is returned instead and the
    exception is suppressed."""
    try:
        return _render_signature(signature(obj), oname)
    except:  # noqa: E722
        return None


def monkey_patch_getdef(self: Any, obj: Any, oname: str = "") -> Union[str, None]:
    try:
        if hasattr(obj, "__ipython_inspector_signature_override__"):
            return _render_signature(
                obj.__ipython_inspector_signature_override__, oname
            )
        return _getdef(self, obj, oname)
    except Exception:
        return None


# try to monkeypatch IPython
try:
    # third party
    from IPython.core.oinspect import Inspector

    if not hasattr(Inspector, "_getdef_bak"):
        Inspector._getdef_bak = Inspector._getdef
        Inspector._getdef = types.MethodType(monkey_patch_getdef, Inspector)
except Exception:
    # print("Failed to monkeypatch IPython Signature Override")
    pass  # nosec


@serializable()
class NodeIdentity(Identity):
    node_name: str

    @staticmethod
    def from_api(api: SyftAPI) -> Optional[NodeIdentity]:
        # stores the name root verify key of the domain node
        if api.connection is not None:
            node_metadata = api.connection.get_node_metadata(api.signing_key)
            return NodeIdentity(
                node_name=node_metadata.name,
                node_id=api.node_uid,
                verify_key=SyftVerifyKey.from_string(node_metadata.verify_key),
            )
        return None

    @classmethod
    def from_change_context(cls, context: ChangeContext) -> NodeIdentity:
        return cls(
            node_name=context.node.name,
            node_id=context.node.id,
            verify_key=context.node.signing_key.verify_key,
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, NodeIdentity):
            return False
        return (
            self.node_name == other.node_name
            and self.verify_key == other.verify_key
            and self.node_id == other.node_id
        )

    def __hash__(self) -> int:
        return hash((self.node_name, self.verify_key))

    def __repr__(self) -> str:
        return f"NodeIdentity <name={self.node_name}, id={self.node_id.short()}, 🔑={str(self.verify_key)[0:8]}>"


def validate_callable_args_and_kwargs(
    args: List, kwargs: Dict, signature: Signature
) -> Union[SyftError, Tuple[List, Dict]]:
    _valid_kwargs = {}
    if "kwargs" in signature.parameters:
        _valid_kwargs = kwargs
    else:
        for key, value in kwargs.items():
            if key not in signature.parameters:
                return SyftError(
                    message=f"""Invalid parameter: `{key}`. Valid Parameters: {list(signature.parameters)}"""
                )
            param = signature.parameters[key]
            if isinstance(param.annotation, str):
                # 🟡 TODO 21: make this work for weird string type situations
                # happens when from __future__ import annotations in a class file
                t = index_syft_by_module_name(param.annotation)
            else:
                t = param.annotation
            msg = None
            try:
                if t is not inspect.Parameter.empty:
                    if isinstance(t, _GenericAlias) and type(None) in t.__args__:
                        success = False
                        for v in t.__args__:
                            if issubclass(v, EmailStr):
                                v = str
                            try:
                                check_type(key, value, v)  # raises Exception
                                success = True
                                break  # only need one to match
                            except Exception:  # nosec
                                pass
                        if not success:
                            raise TypeError()
                    else:
                        check_type(key, value, t)  # raises Exception
            except TypeError:
                _type_str = getattr(t, "__name__", str(t))
                msg = f"`{key}` must be of type `{_type_str}` not `{type(value).__name__}`"

            if msg:
                return SyftError(message=msg)

            _valid_kwargs[key] = value

    # signature.parameters is an OrderedDict, therefore,
    # its fair to assume that order of args
    # and the signature.parameters should always match
    _valid_args = []
    if "args" in signature.parameters:
        _valid_args = args
    else:
        for (param_key, param), arg in zip(signature.parameters.items(), args):
            if param_key in _valid_kwargs:
                continue
            t = param.annotation
            msg = None
            try:
                if t is not inspect.Parameter.empty:
                    if isinstance(t, _GenericAlias) and type(None) in t.__args__:
                        for v in t.__args__:
                            if issubclass(v, EmailStr):
                                v = str
                            check_type(param_key, arg, v)  # raises Exception
                            break  # only need one to match
                    else:
                        check_type(param_key, arg, t)  # raises Exception
            except TypeError:
                t_arg = type(arg)
                if (
                    autoreload_enabled()
                    and t.__module__ == t_arg.__module__
                    and t.__name__ == t_arg.__name__
                ):
                    # ignore error when autoreload_enabled()
                    pass
                else:
                    _type_str = getattr(t, "__name__", str(t))
                    msg = f"Arg: {arg} must be {_type_str} not {type(arg).__name__}"

            if msg:
                return SyftError(message=msg)

            _valid_args.append(arg)

    return _valid_args, _valid_kwargs


RemoteFunction.update_forward_refs()
RemoteUserCodeFunction.update_forward_refs()
