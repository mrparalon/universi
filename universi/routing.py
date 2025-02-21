import datetime
import functools
import inspect
import typing
import warnings
from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import GenericAlias, MappingProxyType, ModuleType
from typing import (
    Any,
    Callable,
    Collection,
    Dict,
    List,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    _BaseGenericAlias,  # pyright: ignore[reportGeneralTypeIssues]
    Union,  # pyright: ignore[reportGeneralTypeIssues]
    cast,
    get_args,
    get_origin,
)
from fastapi import Response, params
from fastapi.datastructures import Default, DefaultPlaceholder
from fastapi.responses import JSONResponse

import fastapi.routing
from fastapi.dependencies.utils import (
    get_body_field,
    get_dependant,
    get_parameterless_sub_dependant,
)
from fastapi.dependencies.models import Dependant
from fastapi.params import Depends
from fastapi.routing import APIRoute
from fastapi.utils import generate_unique_id
from pydantic import BaseModel
from starlette._utils import is_async_callable  # pyright: ignore[reportMissingImports]
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp
from starlette.routing import (
    BaseRoute,
    request_response,
)
from typing_extensions import Self, assert_never, deprecated

from universi._utils import Sentinel, UnionType, get_another_version_of_cls
from universi.codegen import _get_package_path_from_module, _get_version_dir_path
from universi.exceptions import RouteAlreadyExistsError, RouterGenerationError, UniversiError
from universi.structure import Version, VersionBundle
from universi.structure.common import Endpoint, VersionDate
from universi.structure.endpoints import (
    EndpointDidntExistInstruction,
    EndpointExistedInstruction,
    EndpointHadInstruction,
    EndpointWasInstruction,
)
from universi.structure.versions import VersionChange

_T = TypeVar("_T", bound=Callable[..., Any])
# This is a hack we do because we can't guarantee how the user will use the router.
_DELETED_ROUTE_TAG = "_UNIVERSI_DELETED_ROUTE"


@dataclass(slots=True, frozen=True, eq=True)
class _EndpointInfo:
    endpoint_path: str
    endpoint_methods: frozenset[str]


def generate_all_router_versions(
    *routers: fastapi.routing.APIRouter,
    versions: VersionBundle,
    latest_schemas_module: ModuleType | None,
) -> dict[VersionDate, fastapi.routing.APIRouter]:
    for router in routers:
        _add_data_migrations_to_all_routes(router, versions)
    root_router = fastapi.routing.APIRouter()
    for router in routers:
        root_router.include_router(router)
    return _EndpointTransformer(root_router, versions, latest_schemas_module).transform()


class VersionedAPIRouter(fastapi.routing.APIRouter):
    def only_exists_in_older_versions(self, endpoint: _T) -> _T:
        index, route = _get_index_and_route_from_func(self.routes, endpoint)
        if index is None or route is None:
            raise LookupError(
                f'Route not found on endpoint: "{endpoint.__name__}". '
                "Are you sure it's a route and decorators are in the correct order?"
            )
        if _DELETED_ROUTE_TAG in route.tags:
            raise UniversiError(f'The route "{endpoint.__name__}" was already deleted. You can\'t delete it again.')
        route.tags.append(_DELETED_ROUTE_TAG)
        return endpoint

    @deprecated("Use universi.generate_all_router_versions instead")
    def create_versioned_copies(
        self,
        versions: VersionBundle,
        *,
        latest_schemas_module: ModuleType | None,
    ) -> dict[VersionDate, fastapi.routing.APIRouter]:
        _add_data_migrations_to_all_routes(self, versions)
        return _EndpointTransformer(self, versions, latest_schemas_module).transform()


class _EndpointTransformer:
    def __init__(
        self,
        parent_router: fastapi.routing.APIRouter,
        versions: VersionBundle,
        latest_schemas_module: ModuleType | None,
    ) -> None:
        self.parent_router = parent_router
        self.versions = versions
        if latest_schemas_module is not None:
            self.annotation_transformer = _AnnotationTransformer(latest_schemas_module, versions)
        else:
            self.annotation_transformer = None

        self.routes_that_never_existed = [
            route for route in parent_router.routes if isinstance(route, APIRoute) and _DELETED_ROUTE_TAG in route.tags
        ]

    def transform(self):
        router = self.parent_router
        routers: dict[VersionDate, fastapi.routing.APIRouter] = {}
        for version in self.versions:
            if self.annotation_transformer:
                self.annotation_transformer.migrate_router_to_version(router, version)

            routers[version.value] = router
            router = deepcopy(router)
            self._apply_endpoint_changes_to_router(router, version)

            routers[version.value].routes = [
                route
                for route in routers[version.value].routes
                if not (isinstance(route, fastapi.routing.APIRoute) and _DELETED_ROUTE_TAG in route.tags)
            ]
        if self.routes_that_never_existed:
            raise RouterGenerationError(
                "Every route you mark with "
                f"@VersionedAPIRouter.{VersionedAPIRouter.only_exists_in_older_versions.__name__} "
                "must be restored in one of the older versions. Otherwise you just need to delete it altogether. "
                "The following routes have been marked with that decorator but were never restored: "
                f"{self.routes_that_never_existed}",
            )
        return routers

    def _apply_endpoint_changes_to_router(
        self,
        router: fastapi.routing.APIRouter,
        version: Version,
    ):  # noqa: C901
        routes = router.routes
        for version_change in version.version_changes:
            for instruction in version_change.alter_endpoint_instructions:
                original_routes = _get_routes(
                    routes,
                    instruction.endpoint_path,
                    instruction.endpoint_methods,
                    instruction.endpoint_func_name,
                    is_deleted=False,
                )
                methods_to_which_we_applied_changes = set()
                methods_we_should_have_applied_changes_to = set(instruction.endpoint_methods)

                if isinstance(instruction, EndpointDidntExistInstruction):
                    # TODO: Optimize me
                    deleted_routes = _get_routes(
                        routes,
                        instruction.endpoint_path,
                        instruction.endpoint_methods,
                        instruction.endpoint_func_name,
                        is_deleted=True,
                    )
                    if deleted_routes:
                        method_union = set()
                        for deleted_route in deleted_routes:
                            method_union |= deleted_route.methods
                        raise RouterGenerationError(
                            f'Endpoint "{list(method_union)} {instruction.endpoint_path}" you tried to delete in '
                            f'"{version_change.__name__}" was already deleted in a newer version. If you really have '
                            f'two routes with the same paths and methods, please, use "endpoint(..., func_name=...)" '
                            f"to distinguish between them. Function names of endpoints that were already deleted: "
                            f"{[r.endpoint.__name__ for r in deleted_routes]}",
                        )
                    for original_route in original_routes:
                        methods_to_which_we_applied_changes |= original_route.methods
                        original_route.tags.append(_DELETED_ROUTE_TAG)
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to delete in'
                        ' "{version_change_name}" doesn\'t exist in a newer version'
                    )
                elif isinstance(instruction, EndpointExistedInstruction):
                    # TODO: Optimize me
                    if original_routes:
                        method_union = set()
                        for original_route in original_routes:
                            method_union |= original_route.methods
                        raise RouterGenerationError(
                            f'Endpoint "{list(method_union)} {instruction.endpoint_path}" you tried to restore in'
                            f' "{version_change.__name__}" already existed in a newer version. If you really have two '
                            f'routes with the same paths and methods, please, use "endpoint(..., func_name=...)" to '
                            f"distinguish between them. Function names of endpoints that already existed: "
                            f"{[r.endpoint.__name__ for r in original_routes]}",
                        )
                    deleted_routes = _get_routes(
                        routes,
                        instruction.endpoint_path,
                        instruction.endpoint_methods,
                        instruction.endpoint_func_name,
                        is_deleted=True,
                    )
                    try:
                        _validate_no_repetitions_in_routes(deleted_routes)
                    except RouteAlreadyExistsError as e:
                        raise RouterGenerationError(
                            f'Endpoint "{list(instruction.endpoint_methods)} {instruction.endpoint_path}" you tried '
                            f'to restore in "{version_change.__name__}" has different applicable routes that could '
                            f"be restored. If you really have two routes with the same paths and methods, please, use "
                            f'"endpoint(..., func_name=...)" to distinguish between them. Function names of '
                            f"endpoints that can be restored: {[r.endpoint.__name__ for r in e.routes]}",
                        ) from e
                    for deleted_route in deleted_routes:
                        methods_to_which_we_applied_changes |= deleted_route.methods
                        deleted_route.tags.remove(_DELETED_ROUTE_TAG)

                        if deleted_route in self.routes_that_never_existed:
                            self.routes_that_never_existed.remove(deleted_route)
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to restore in'
                        ' "{version_change_name}" wasn\'t among the deleted routes'
                    )
                elif isinstance(instruction, EndpointHadInstruction):
                    for original_route in original_routes:
                        methods_to_which_we_applied_changes |= original_route.methods
                        _apply_endpoint_had_instruction(version_change, instruction, original_route)
                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" you tried to change in'
                        ' "{version_change_name}" doesn\'t exist'
                    )
                elif isinstance(instruction, EndpointWasInstruction):
                    # TODO: Add test for changing dependant and checking that the schemas used in the dependant have been migrated to the correct version
                    for original_route in original_routes:
                        methods_to_which_we_applied_changes |= original_route.methods
                        original_response_model = original_route.endpoint.response_model

                        original_route.endpoint = instruction.get_old_endpoint()
                        _remake_endpoint_dependencies(original_route)
                        original_dependant = original_route.dependant
                        if self.annotation_transformer:
                            version_dir = _get_version_dir_path(
                                self.annotation_transformer.latest_schemas_module, version.value
                            )
                            self.annotation_transformer.migrate_route_to_version(
                                original_route, version_dir, ignore_response_model=True
                            )

                        _add_data_migrations_to_route(
                            original_route, original_response_model, original_dependant, self.versions
                        )

                    err = (
                        'Endpoint "{endpoint_methods} {endpoint_path}" whose handler you tried to change in'
                        ' "{version_change_name}" doesn\'t exist'
                    )
                else:
                    assert_never(instruction)
                method_diff = methods_we_should_have_applied_changes_to - methods_to_which_we_applied_changes
                if method_diff:
                    # ERR
                    raise RouterGenerationError(
                        err.format(
                            endpoint_methods=list(method_diff),
                            endpoint_path=instruction.endpoint_path,
                            version_change_name=version_change.__name__,
                        ),
                    )


def _validate_no_repetitions_in_routes(routes: list[fastapi.routing.APIRoute]):
    route_map = {}

    for route in routes:
        route_info = _EndpointInfo(route.path, frozenset(route.methods))
        if route_info in route_map:
            raise RouteAlreadyExistsError(route, route_map[route_info])
        route_map[route_info] = route


class _AnnotationTransformer:
    __slots__ = (
        "latest_schemas_module",
        "version_dirs",
        "template_version_dir",
        "latest_version_dir",
        "change_versions_of_a_non_container_annotation",
    )

    def __init__(self, latest_schemas_module: ModuleType, versions: VersionBundle) -> None:
        if not hasattr(latest_schemas_module, "__path__"):
            raise RouterGenerationError(
                f'The latest schemas module must be a package. "{latest_schemas_module.__name__}" is not a package.',
            )
        if not latest_schemas_module.__name__.endswith(".latest"):
            raise RouterGenerationError(
                'The name of the latest schemas module must be "latest". '
                f'Received "{latest_schemas_module.__name__}" instead.',
            )
        self.latest_schemas_module = latest_schemas_module
        self.version_dirs = frozenset(
            [_get_package_path_from_module(latest_schemas_module)]
            + [_get_version_dir_path(latest_schemas_module, version.value) for version in versions],
        )
        # Okay, the naming is confusing, I know. Essentially template_version_dir is a dir of
        # latest_schemas_module while latest_version_dir is a version equivalent to latest but
        # with its own directory. Pick a better naming and make a PR, I am at your mercy.
        self.template_version_dir = min(self.version_dirs)  # "latest" < "v0000_00_00"
        self.latest_version_dir = max(self.version_dirs)  # "v2005_11_11" > "v2000_11_11"

        # This cache is not here for speeding things up. It's for preventing the creation of copies of the same object
        # because such copies could produce weird behaviors at runtime, especially if you/fastapi do any comparisons.
        # It's defined here and not on the method because of this: https://youtu.be/sVjtp6tGo0g
        self.change_versions_of_a_non_container_annotation = functools.cache(
            self._change_versions_of_a_non_container_annotation,
        )

    def migrate_router_to_version(self, router: fastapi.routing.APIRouter, version: Version):
        version_dir = _get_version_dir_path(self.latest_schemas_module, version.value)
        if not version_dir.is_dir():
            raise RouterGenerationError(
                f"Versioned schema directory '{version_dir}' does not exist.",
            )
        for route in router.routes:
            if not isinstance(route, fastapi.routing.APIRoute):
                continue
            self.migrate_route_to_version(route, version_dir)

    def migrate_route_to_version(
        self, route: fastapi.routing.APIRoute, version_dir: Path, *, ignore_response_model: bool = False
    ):
        if route.response_model is not None and not ignore_response_model:
            route.response_model = self._change_version_of_annotations(route.response_model, version_dir)
        route.dependencies = self._change_version_of_annotations(route.dependencies, version_dir)
        route.endpoint = self._change_version_of_annotations(route.endpoint, version_dir)
        _remake_endpoint_dependencies(route)

    def _change_versions_of_a_non_container_annotation(self, annotation: Any, version_dir: Path) -> Any:
        if isinstance(annotation, _BaseGenericAlias | GenericAlias):
            return get_origin(annotation)[
                tuple(self._change_version_of_annotations(arg, version_dir) for arg in get_args(annotation))
            ]
        elif isinstance(annotation, Depends):
            return Depends(
                self._change_version_of_annotations(annotation.dependency, version_dir),
                use_cache=annotation.use_cache,
            )
        elif isinstance(annotation, UnionType):
            getitem = typing.Union.__getitem__  # pyright: ignore[reportGeneralTypeIssues]
            return getitem(
                tuple(self._change_version_of_annotations(a, version_dir) for a in get_args(annotation)),
            )
        elif annotation is typing.Any or isinstance(annotation, typing.NewType):
            return annotation
        elif isinstance(annotation, type):
            return self._change_version_of_type(annotation, version_dir)
        elif callable(annotation):
            if inspect.iscoroutinefunction(annotation):

                @functools.wraps(annotation)
                async def new_callable(  # pyright: ignore[reportGeneralTypeIssues]
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    return await annotation(*args, **kwargs)

            else:

                @functools.wraps(annotation)
                def new_callable(  # pyright: ignore[reportGeneralTypeIssues]
                    *args: Any,
                    **kwargs: Any,
                ) -> Any:
                    return annotation(*args, **kwargs)

            # Otherwise it will have the same signature as __wrapped__
            del new_callable.__wrapped__
            old_params = inspect.signature(annotation).parameters
            callable_annotations = new_callable.__annotations__

            new_callable: Any = cast(Any, new_callable)
            new_callable.__annotations__ = self._change_version_of_annotations(
                callable_annotations,
                version_dir,
            )
            new_callable.__defaults__ = self._change_version_of_annotations(
                tuple(p.default for p in old_params.values() if p.default is not inspect.Signature.empty),
                version_dir,
            )
            new_callable.__signature__ = _generate_signature(new_callable, old_params)
            return new_callable
        else:
            return annotation

    def _change_version_of_annotations(self, annotation: Any, version_dir: Path) -> Any:
        """Recursively go through all annotations and if they were taken from any versioned package, change them to the
        annotations corresponding to the version_dir passed.

        So if we had a annotation "UserResponse" from "latest" version, and we passed version_dir of "v1_0_1", it would
        replace "UserResponse" with the the same class but from the "v1_0_1" version.

        """
        if isinstance(annotation, dict):
            return {
                self._change_version_of_annotations(key, version_dir): self._change_version_of_annotations(
                    value,
                    version_dir,
                )
                for key, value in annotation.items()
            }

        elif isinstance(annotation, (list, tuple)):
            return type(annotation)(self._change_version_of_annotations(v, version_dir) for v in annotation)
        else:
            return self.change_versions_of_a_non_container_annotation(annotation, version_dir)

    def _change_version_of_type(self, annotation: type, version_dir: Path):
        if issubclass(annotation, BaseModel | Enum):
            if version_dir == self.latest_version_dir:
                source_file = inspect.getsourcefile(annotation)
                if source_file is None:  # pragma: no cover # I am not even sure how to cover this
                    warnings.warn(
                        f'Failed to find where the type annotation "{annotation}" is located.'
                        "Please, double check that it's located in the right directory",
                        stacklevel=7,
                    )
                else:
                    template_dir = str(self.template_version_dir)
                    dir_with_versions = str(self.template_version_dir.parent)

                    # So if it is somewhere close to version dirs (either within them or next to them),
                    # but not located in "latest",
                    # but also not located in any other version dir
                    if (
                        source_file.startswith(dir_with_versions)
                        and not source_file.startswith(template_dir)
                        and any(source_file.startswith(str(d)) for d in self.version_dirs)
                    ):
                        raise RouterGenerationError(
                            f'"{annotation}" is not defined in "{self.template_version_dir}" even though it must be. '
                            f'It is defined in "{Path(source_file).parent}". '
                            "It probably means that you used a specific version of the class in fastapi dependencies "
                            'or pydantic schemas instead of "latest".',
                        )
            return get_another_version_of_cls(annotation, version_dir, self.version_dirs)
        else:
            return annotation


def _remake_endpoint_dependencies(route: fastapi.routing.APIRoute):
    route.dependant = get_dependant(path=route.path_format, call=route.endpoint)
    route.body_field = get_body_field(dependant=route.dependant, name=route.unique_id)
    for depends in route.dependencies[::-1]:
        route.dependant.dependencies.insert(
            0,
            get_parameterless_sub_dependant(depends=depends, path=route.path_format),
        )
    route.app = request_response(route.get_route_handler())


def _add_data_migrations_to_all_routes(router: fastapi.routing.APIRouter, versions: VersionBundle):
    for route in router.routes:
        if isinstance(route, fastapi.routing.APIRoute):
            _add_data_migrations_to_route(route, route.response_model, route.dependant, versions)


def _add_data_migrations_to_route(route: BaseRoute, response_model: Any, dependant: Dependant, versions: VersionBundle):
    if isinstance(route, fastapi.routing.APIRoute):
        if not is_async_callable(route.endpoint):
            raise RouterGenerationError("All versioned endpoints must be asynchronous.")
        route.endpoint = versions.versioned(response_model, body_params=dependant.body_params)(route.endpoint)


def _apply_endpoint_had_instruction(
    version_change: type[VersionChange],
    instruction: EndpointHadInstruction,
    original_route: APIRoute,
):
    for attr_name in instruction.attributes.__dataclass_fields__:
        attr = getattr(instruction.attributes, attr_name)
        if attr is not Sentinel:
            if getattr(original_route, attr_name) == attr:
                raise RouterGenerationError(
                    f'Expected attribute "{attr_name}" of endpoint'
                    f' "{list(original_route.methods)} {original_route.path}"'
                    f' to be different in "{version_change.__name__}", but it was the same.'
                    " It means that your version change has no effect on the attribute"
                    " and can be removed.",
                )
            setattr(original_route, attr_name, attr)


def _generate_signature(
    new_callable: Callable,
    old_params: MappingProxyType[str, inspect.Parameter],
):
    parameters = []
    default_counter = 0
    for param in old_params.values():
        if param.default is not inspect.Signature.empty:
            default = new_callable.__defaults__[default_counter]
            default_counter += 1
        else:
            default = inspect.Signature.empty
        parameters.append(
            inspect.Parameter(
                param.name,
                param.kind,
                default=default,
                annotation=new_callable.__annotations__.get(
                    param.name,
                    inspect.Signature.empty,
                ),
            ),
        )
    return inspect.Signature(
        parameters=parameters,
        return_annotation=new_callable.__annotations__.get(
            "return",
            inspect.Signature.empty,
        ),
    )


def _get_routes(
    routes: Sequence[BaseRoute],
    endpoint_path: str,
    endpoint_methods: Collection[str],
    endpoint_func_name: str | None = None,
    *,
    is_deleted: bool = False,
) -> list[fastapi.routing.APIRoute]:
    found_routes = []
    endpoint_method_set = set(endpoint_methods)
    for route in routes:
        if (
            isinstance(route, fastapi.routing.APIRoute)
            and route.path == endpoint_path
            and set(route.methods).issubset(endpoint_method_set)
            and (endpoint_func_name is None or route.endpoint.__name__ == endpoint_func_name)
            and (_DELETED_ROUTE_TAG in route.tags) == is_deleted
        ):
            found_routes.append(route)
    return found_routes


def _get_index_and_route_from_func(
    routes: Sequence[BaseRoute],
    endpoint: Endpoint,
) -> tuple[int, fastapi.routing.APIRoute] | tuple[None, None]:
    for index, route in enumerate(routes):
        if isinstance(route, fastapi.routing.APIRoute) and (
            route.endpoint == endpoint or getattr(route.endpoint, "func", None) == endpoint
        ):
            return index, route
    return None, None
