# -*- coding: utf-8 -*-
"""
    emmett_rest.rest
    ----------------

    Provides main REST logics

    :copyright: 2017 Giovanni Barillari
    :license: BSD-3-Clause
"""

from __future__ import annotations

import operator

from emmett import AppModule, Pipe, request, response, sdict
from emmett.tools import ServicePipe
from functools import reduce
from typing import Generic, List, Optional

from .helpers import DEFAULT, RecordFetcher, SetFetcher
from .parsers import (
    parse_params as _parse_params,
    parse_params_with_parser as _parse_params_wparser
)
from .queries import JSONQueryPipe
from .serializers import serialize as _serialize


class RESTModule(AppModule):
    _all_methods = {'index', 'create', 'read', 'update', 'delete'}

    @classmethod
    def from_app(
        cls,
        ext: Generic,
        import_name: str,
        name: str,
        model: Generic,
        serializer: Optional[Generic] = None,
        parser: Optional[Generic] = None,
        enabled_methods: Optional[List] = None,
        disabled_methods: Optional[List] = None,
        list_envelope: Optional[str] = None,
        single_envelope: Optional[str] = DEFAULT,
        meta_envelope: Optional[str] = DEFAULT,
        use_envelope_on_parse: Optional[bool] = None,
        serialize_meta: Optional[bool] = None,
        url_prefix: Optional[str] = None,
        hostname: Optional[str] = None
    ) -> RESTModule:
        return cls(
            ext, name, import_name, model, serializer, parser,
            enabled_methods, disabled_methods,
            list_envelope, single_envelope, meta_envelope,
            use_envelope_on_parse, serialize_meta,
            url_prefix, hostname
        )

    @classmethod
    def from_module(
        cls,
        ext: Generic,
        mod: AppModule,
        import_name: str,
        name: str,
        model: Generic,
        serializer: Optional[Generic] = None,
        parser: Optional[Generic] = None,
        enabled_methods: Optional[List] = None,
        disabled_methods: Optional[List] = None,
        list_envelope: Optional[str] = None,
        single_envelope: Optional[str] = DEFAULT,
        meta_envelope: Optional[str] = DEFAULT,
        use_envelope_on_parse: Optional[bool] = None,
        serialize_meta: Optional[bool] = None,
        url_prefix: Optional[str] = None,
        hostname: Optional[str] = None
    ) -> RESTModule:
        if '.' in name:
            raise RuntimeError(
                "Nested app modules' names should not contains dots"
            )
        name = mod.name + '.' + name
        if url_prefix and not url_prefix.startswith('/'):
            url_prefix = '/' + url_prefix
        module_url_prefix = (mod.url_prefix + (url_prefix or '')) \
            if mod.url_prefix else url_prefix
        hostname = hostname or mod.hostname
        return cls(
            ext, name, import_name, model, serializer, parser,
            enabled_methods, disabled_methods,
            list_envelope, single_envelope, meta_envelope,
            use_envelope_on_parse, serialize_meta,
            module_url_prefix, hostname, mod.pipeline
        )

    def __init__(
        self, ext, name, import_name, model, serializer=None, parser=None,
        enabled_methods=None, disabled_methods=None,
        list_envelope=None, single_envelope=DEFAULT, meta_envelope=DEFAULT,
        use_envelope_on_parse=None, serialize_meta=None,
        url_prefix=None, hostname=None, pipeline=[]
    ):
        #: overridable methods
        self._fetcher_method = self._get_dbset
        self._select_method = self._get_row
        self._after_parse_method = self._after_parse_params
        self.error_400 = self.build_error_400
        self.error_404 = self.build_error_404
        self.error_422 = self.build_error_422
        self.build_meta = self._build_meta
        #: service pipe injection
        add_service_pipe = True
        super_pipeline = list(pipeline)
        if any(
            isinstance(pipe, ServicePipe) for pipe in ext.app.pipeline
        ) or any(
            isinstance(pipe, ServicePipe) for pipe in super_pipeline
        ):
            add_service_pipe = False
        if add_service_pipe:
            super_pipeline.insert(0, ServicePipe('json'))
        #: initialize
        super(RESTModule, self).__init__(
            ext.app, name, import_name,
            url_prefix=url_prefix,
            hostname=hostname,
            pipeline=super_pipeline
        )
        self.ext = ext
        self._pagination = sdict()
        for key in (
            'page_param', 'pagesize_param',
            'min_pagesize', 'max_pagesize', 'default_pagesize'
        ):
            self._pagination[key] = self.ext.config[key]
        self._sort_param = self.ext.config.sort_param
        self.default_sort = self.ext.config.default_sort
        self._path_base = self.ext.config.base_path
        self._path_rid = self.ext.config.id_path
        self._serializer_class = serializer or \
            self.ext.config.default_serializer
        self._parser_class = parser or self.ext.config.default_parser
        self._parsing_params_kwargs = {}
        self.model = model
        self.serializer = self._serializer_class(self.model)
        self.parser = self._parser_class(self.model)
        self.enabled_methods = list(self._all_methods & set(
            list(
                enabled_methods if enabled_methods is not None else
                self.ext.config.default_enabled_methods
            )
        ))
        self.disabled_methods = list(self._all_methods & set(
            list(
                disabled_methods if disabled_methods is not None else
                self.ext.config.default_disabled_methods
            )
        ))
        self.list_envelope = list_envelope or self.ext.config.list_envelope
        self.single_envelope = (
            single_envelope if single_envelope is not DEFAULT else
            self.ext.config.single_envelope
        )
        self.meta_envelope = (
            meta_envelope if meta_envelope is not DEFAULT else
            self.ext.config.meta_envelope
        )
        self.use_envelope_on_parse = (
            use_envelope_on_parse if use_envelope_on_parse is not None else
            self.config.use_envelope_on_parse
        )
        self._queryable_fields = []
        self._sortable_fields = []
        self._sortable_dict = {}
        self._json_query_pipe = JSONQueryPipe(self)
        self.allowed_sorts = [self.default_sort]
        self.index_pipeline = [SetFetcher(self), self._json_query_pipe]
        self.create_pipeline = []
        self.read_pipeline = [SetFetcher(self), RecordFetcher(self)]
        self.update_pipeline = [SetFetcher(self)]
        self.delete_pipeline = [SetFetcher(self)]
        #: custom init
        self.init()
        #: configure module
        self._after_initialize()

    def init(self):
        pass

    def _after_initialize(self):
        self.list_envelope = self.list_envelope or 'data'
        #: adjust single row serialization based on evenlope
        self.serialize_many = (
            self.serialize_with_list_envelope_and_meta if self.serialize_meta
            else self.serialize_with_list_envelope)
        if self.single_envelope:
            self.serialize_one = self.serialize_with_single_envelope
            if self.use_envelope_on_parse:
                self.parser.envelope = self.single_envelope
                self._parsing_params_kwargs = \
                    {'evenlope': self.single_envelope}
        else:
            self.serialize_one = self.serialize
        #: adjust enabled methods
        for method_name in self.disabled_methods:
            self.enabled_methods.remove(method_name)
        #: route enabled methods
        self._expose_routes()

    def _expose_routes(self):
        self._methods_map = {
            'index': (self._path_base, 'get'),
            'read': (self._path_rid, 'get'),
            'create': (self._path_base, 'post'),
            'update': (self._path_rid, ['put', 'patch']),
            'delete': (self._path_rid, 'delete')
            # TODO: additional methods
        }
        for key in self.enabled_methods:
            path, methods = self._methods_map[key]
            pipeline = getattr(self, key + "_pipeline")
            f = getattr(self, "_" + key)
            self.route(path, pipeline=pipeline, methods=methods, name=key)(f)

    def _get_dbset(self):
        return self.model.all()

    def _get_row(self, dbset):
        return dbset.select(limitby=(0, 1)).first()

    def get_pagination(self):
        try:
            page = int(request.query_params[self._pagination.page_param] or 1)
            assert page > 0
        except Exception:
            page = 1
        try:
            page_size = int(
                request.query_params[self._pagination.pagesize_param] or 20)
            assert (
                self._pagination.min_pagesize <= page_size <=
                self._pagination.max_pagesize)
        except Exception:
            page_size = self._pagination.default_pagesize
        return page, page_size

    def get_sort(self):
        pfields = (
            (
                isinstance(request.query_params.sort_by, str) and
                request.query_params.sort_by
            ) or self.default_sort
        ).split(',')
        rv = []
        for pfield in pfields:
            asc = True
            if pfield.startswith('-'):
                pfield = pfield[1:]
                asc = False
            field = self._sortable_dict.get(pfield)
            if not field:
                continue
            rv.append(field if asc else ~field)
        return reduce(
            lambda a, b: operator.or_(a, b) if a and b else None,
            rv
        )

    def build_error_400(self, errors=None):
        if errors:
            return {'errors': errors}
        return {'errors': {'request': 'bad request'}}

    def build_error_404(self):
        return {'errors': {'id': 'record not found'}}

    def build_error_422(self, errors=None, to_dict=True):
        if errors:
            if to_dict:
                errors = errors.as_dict()
            return {'errors': errors}
        return {'errors': {'request': 'unprocessable entity'}}

    def _build_meta(self, dbset, pagination):
        count = dbset.count()
        page, page_size = pagination
        return {
            'object': 'list',
            'has_more': count > (page * page_size),
            'total_objects': count
        }

    def serialize(self, data, **extras):
        return _serialize(data, self.serializer, **extras)

    def serialize_with_list_envelope(self, data, **extras):
        return {self.list_envelope: self.serialize(data, **extras)}

    def serialize_with_list_envelope_and_meta(
        self, data, dbset, pagination, **extras
    ):
        return {
            self.list_envelope: self.serialize(data, **extras),
            self.meta_envelope: self.build_meta
        }

    def serialize_with_single_envelope(self, data, **extras):
        return {self.single_envelope: self.serialize(data, **extras)}

    async def parse_params(self, *params):
        if params:
            rv = await _parse_params(*params, **self._parsing_params_kwargs)
        else:
            rv = await _parse_params_wparser(self.parser)
        self._after_parse_params(rv)
        return rv

    def _after_parse_params(self, attrs):
        pass

    #: default routes
    async def _index(self, dbset):
        pagination = self.get_pagination()
        sort = self.get_sort()
        rows = dbset.select(paginate=pagination, orderby=sort)
        return self.serialize_many(rows, dbset, pagination)

    async def _read(self, row):
        return self.serialize_one(row)

    async def _create(self):
        response.status = 201
        attrs = await self.parse_params()
        r = self.model.create(**attrs)
        if r.errors:
            response.status = 422
            return self.error_422(r.errors)
        return self.serialize_one(r.id)

    async def _update(self, dbset, rid):
        attrs = await self.parse_params()
        r = dbset.where(self.model.id == rid).validate_and_update(**attrs)
        if r.errors:
            response.status = 422
            return self.error_422(r.errors)
        elif not r.updated:
            response.status = 404
            return self.error_404()
        return self.serialize_one(self.model.get(rid))

    async def _delete(self, dbset, rid):
        rv = dbset.where(self.model.id == rid).delete()
        if not rv:
            response.status = 404
            return self.error_404()
        return {}

    #: props
    @property
    def pipeline(self) -> List[Pipe]:
        return self._pipeline

    @pipeline.setter
    def pipeline(self, val: List[Pipe]):
        raise RuntimeError(
            'Cannot change the pipeline of REST modules directly.'
            'Please use an upper module for this.'
        )

    @property
    def allowed_sorts(self) -> List[str]:
        return self._sortable_fields

    @allowed_sorts.setter
    def allowed_sorts(self, val: List[str]):
        self._sortable_fields = val
        self._sortable_dict = {
            field: self.model.table[field] for field in self._sortable_fields
        }

    @property
    def query_allowed_fields(self) -> List[str]:
        return self._queryable_fields

    @query_allowed_fields.setter
    def query_allowed_fields(self, val: List[str]):
        self._queryable_fields = val
        self._jsonquery_pipe.set_accepted()

    #: decorators
    def get_dbset(self, f):
        self._fetcher_method = f
        return f

    def get_row(self, f):
        self._select_method = f
        return f

    def after_parse_params(self, f):
        self._after_parse_method = f
        return f

    def index(self, pipeline=[]):
        pipeline = self.index_pipeline + pipeline
        return self.route(
            self._path_base, pipeline=pipeline, methods='get', name='index'
        )

    def read(self, pipeline=[]):
        pipeline = self.read_pipeline + pipeline
        return self.route(
            self._path_rid, pipeline=pipeline, methods='get', name='read'
        )

    def create(self, pipeline=[]):
        pipeline = self.create_pipeline + pipeline
        return self.route(
            self._path_base, pipeline=pipeline, methods='post', name='create'
        )

    def update(self, pipeline=[]):
        pipeline = self.update_pipeline + pipeline
        return self.route(
            self._path_rid, pipeline=pipeline, methods=['put', 'patch'],
            name='update'
        )

    def delete(self, pipeline=[]):
        pipeline = self.delete_pipeline + pipeline
        return self.route(
            self._path_rid, pipeline=pipeline, methods='delete', name='delete'
        )

    def on_400(self, f):
        self.error_400 = f
        return f

    def on_404(self, f):
        self.error_404 = f
        return f

    def on_422(self, f):
        self.error_422 = f
        return f

    def meta_builder(self, f):
        self.build_meta = f
        return f
