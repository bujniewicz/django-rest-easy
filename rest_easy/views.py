# coding: utf-8
# pylint: disable=too-few-public-methods
"""
This module provides redefined DRF's generic views and viewsets leveraging serializer registration.

One of the main issues with creating traditional DRF APIs is a lot of bloat (and we're writing Python, not Java or C#,
to avoid bloat) that's completely unnecessary in a structured Django project. Therefore, this module aims to provide
a better and simpler way to write simple API endpoints - without limiting the ability to create more complex views.
The particular means to that end are:

* :class:`rest_easy.scopes.ScopeQuerySet` and its subclasses (:class:`rest_easy.scopes.UrlKwargScopeQuerySet` and
  :class:`rest_easy.scopes.RequestAttrScopeQuerySet`) provide a simple way to scope views and viewsets.
  by resource (ie. limiting results to single account, or /resource/<resource_pk>/inner_resource/<inner_resource_pk>/)
* generic views leveraging the above, as well as model-and-schema specification instead of queryset, serializer and
  helper methods - all generic views that were available in DRF as well as GenericAPIView are redefined to support
  this.
* Generic :class:`rest_easy.views.ModelViewSet` which allows for very simple definition of resource
  endpoint.

To make the new views work, all that\'s required is a serializer::

    from users.models import User
    from accounts.models import Account
    from rest_easy.serializers import ModelSerializer
    class UserSerializer(ModelSerializer):
        class Meta:
            model = User
            fields = '__all__'
            schema = 'default'

    class UserViewSet(ModelViewSet):
        model = User
        scope = UrlKwargScopeQuerySet(Account)

and in urls.py::

    from django.conf.urls import url, include
    from rest_framework.routers import DefaultRouter
    router = DefaultRouter()
    router.register(r'accounts/(?P<account_pk>[0-9]+)/users', UserViewSet)
    urlpatterns = [url(r'^', include(router.urls))]

The above will provide the users scoped by account primary key as resources: with list, retrieve, create, update and
partial update methods, as well as standard HEAD and OPTIONS autogenerated responses.

You can easily add custom paths to viewsets when needed - it's described in DRF documentation.
"""

from django.conf import settings
from rest_framework.viewsets import ViewSetMixin
from rest_framework import generics, mixins
from six import with_metaclass

from rest_easy.exceptions import RestEasyException
from rest_easy.registers import serializer_register
from rest_easy.scopes import ScopeQuerySet

__all__ = ['GenericAPIView', 'CreateAPIView', 'ListAPIView', 'RetrieveAPIView', 'DestroyAPIView', 'UpdateAPIView',
           'ListCreateAPIView', 'RetrieveUpdateAPIView', 'RetrieveDestroyAPIView', 'RetrieveUpdateDestroyAPIView',
           'ReadOnlyModelViewSet', 'ModelViewSet']


def get_additional_bases():
    """
    Looks for additional view bases in settings.REST_EASY_VIEW_BASES.
    :return:
    """
    resolved_bases = []
    from importlib import import_module
    for base in getattr(settings, 'REST_EASY_VIEW_BASES', []):
        mod, cls = base.rsplit('.', 1)
        resolved_bases.append(getattr(import_module(mod), cls))

    return resolved_bases


def get_additional_mixins():
    """
    Looks for additional view bases in settings.REST_EASY_VIEW_MIXINS.
    :return:
    """
    resolved_bases = []
    from importlib import import_module
    for base in getattr(settings, 'REST_EASY_GENERIC_VIEW_MIXINS', []):
        mod, cls = base.rsplit('.', 1)
        resolved_bases.append(getattr(import_module(mod), cls))

    return resolved_bases

ADDITIONAL_MIXINS = get_additional_mixins()


class ScopedViewMixin(object):
    """
    This class provides a get_queryset method that works with ScopeQuerySet.

    Queryset obtained from superclass is filtered by view.scope's (if it exists) child_queryset() method.
    """

    def get_queryset(self):
        """
        Calls scope's child_queryset methods on queryset as obtained from superclass.
        :return: queryset.
        """
        queryset = super(ScopedViewMixin, self).get_queryset()
        if hasattr(self, 'scope') and self.scope:
            for scope in self.scope:
                queryset = scope.child_queryset(queryset, self)
        return queryset

    def get_scoped_object(self, handle):
        """
        Obtains object from scope when scope's get_object_handle was set.
        :param handle: get_object_handle used in scope initialization.
        :return: object used by scope to filter.
        """
        scope = self.rest_easy_available_object_handles.get(handle, None)
        if scope:
            return scope.get_object(self)
        raise AttributeError('{} get_object handle not found on object {}'.format(handle, self))

    def __getattr__(self, item):
        """
        A shortcut providing get_{get_object_handle} to be able to easily access objects used by this view's scopes
        for filtering. For example, scope = UrlKwargScopeQuerySet(Account) will be available with self.get_account().
        :param item: item to obtain plus 'get_' prefix
        :return: object used by scope for filtering.
        """
        if not item.startswith('get_'):
            raise AttributeError('{} not found on object {}'.format(item, self))
        handle = item[4:]
        try:
            return self.get_scoped_object(handle)
        except AttributeError:
            raise AttributeError('{} not found on object {}'.format(item, self))


class ViewEasyMetaclass(type):  # pylint: disable=too-few-public-methods
    """
    This metaclass sets default queryset on a model-and-schema based views and fills in concrete views with bases.

    It's required for compatibility with some of DRF's elements, like routers.
    """

    def __new__(mcs, name, bases, attrs):
        """
        Create the class.
        """
        if ('queryset' not in attrs or attrs['queryset'] is None) and 'model' in attrs:
            attrs['queryset'] = attrs['model'].objects.all()
        if 'scope' in attrs and isinstance(attrs['scope'], ScopeQuerySet):
            attrs['scope'] = [attrs['scope']]
        attrs['rest_easy_available_object_handles'] = {}
        cls = super(ViewEasyMetaclass, mcs).__new__(mcs, name, bases, attrs)
        for scope in getattr(cls, 'scope', []):
            scope.contribute_to_class(cls)
        return cls


class ChainingCreateUpdateMixin(object):
    """
    Chain-enabled versions of perform_create and perform_update.
    """

    def perform_create(self, serializer, **kwargs):  # pylint: disable=no-self-use
        """
        Extend default implementation with kwarg chaining.
        """
        return serializer.save(**kwargs)

    def perform_update(self, serializer, **kwargs):  # pylint: disable=no-self-use
        """
        Extend default implementation with kwarg chaining.
        """
        return serializer.save(**kwargs)


class GenericAPIViewBase(ScopedViewMixin, generics.GenericAPIView):
    """
    Provides a base for all generic views and viewsets leveraging registered serializers and ScopeQuerySets.

    Adds additional DRF-verb-wise override for obtaining serializer class: serializer_schema_for_verb property.
    It should be a dictionary of DRF verbs and serializer schemas (they work in conjunction with model property).
        serializer_schema_for_verb = {'update': 'schema-mutate', 'create': 'schema-mutate'}
    The priority for obtaining serializer class is:

    * get_serializer_class override
    * serializer_class property
    * model + serializer_schema_for_verb[verb] lookup in :class:`rest_easy.registers.SerializerRegister`
    * model + schema lookup in :class:`rest_easy.registers.SerializerRegister`

    """
    serializer_schema_for_verb = {}

    def __init__(self, **kwargs):
        """
        Set object cache to empty dict.
        :param kwargs: Passthrough to Django view.
        """
        super(GenericAPIViewBase, self).__init__(**kwargs)
        self.rest_easy_object_cache = {}

    def get_drf_verb(self):
        """
        Obtain the DRF verb used for a request.
        """
        method = self.request.method.lower()
        if method == 'get':
            if self.lookup_url_kwarg in self.kwargs:
                return 'retrieve'
        mapping = {
            'get': 'list',
            'post': 'create',
            'put': 'update',
            'patch': 'partial_update',
            'delete': 'destroy'
        }
        return mapping[method]

    def get_serializer_name(self, verb=None):
        """
        Obtains registered serializer name for this view.

        Leverages :class:`rest_easy.registers.SerializerRegister`. Works when either of or both model
        and schema properties are available on this view.

        :return: registered serializer key.
        """
        model = getattr(self, 'model', None)
        schema = None
        if not model and not hasattr(self, 'schema') and (verb and verb not in self.serializer_schema_for_verb):
            raise RestEasyException('Either model or schema fields need to be set on a model-based GenericAPIView.')
        if verb:
            schema = self.serializer_schema_for_verb.get(verb, None)
        if schema is None:
            schema = getattr(self, 'schema', 'default')
        return serializer_register.get_name(model, schema)

    def get_serializer_class(self):
        """
        Gets serializer appropriate for this view.

        Leverages :class:`rest_easy.registers.SerializerRegister`. Works when either of or both model
        and schema properties are available on this view.

        :return: serializer class.
        """

        if hasattr(self, 'serializer_class') and self.serializer_class:
            return self.serializer_class

        serializer = serializer_register.lookup(self.get_serializer_name(verb=self.get_drf_verb()))
        if serializer:
            return serializer

        raise RestEasyException(u'Serializer for model {} and schema {} cannot be found.'.format(
            getattr(self, 'model', '[no model]'),
            getattr(self, 'schema', '[no schema]')
        ))


class GenericAPIView(with_metaclass(ViewEasyMetaclass, *(get_additional_bases() + [GenericAPIViewBase]))):
    """
    Base view with compat metaclass.
    """
    __abstract__ = True


def create(self, request, *args, **kwargs):  # pragma: no cover
    """
    Shortcut method.
    """
    return self.create(request, *args, **kwargs)


def list_(self, request, *args, **kwargs):  # pragma: no cover
    """
    Shortcut method.
    """
    return self.list(request, *args, **kwargs)


def retrieve(self, request, *args, **kwargs):  # pragma: no cover
    """
    Shortcut method.
    """
    return self.retrieve(request, *args, **kwargs)


def destroy(self, request, *args, **kwargs):  # pragma: no cover
    """
    Shortcut method.
    """
    return self.destroy(request, *args, **kwargs)


def update(self, request, *args, **kwargs):  # pragma: no cover
    """
    Shortcut method.
    """
    return self.update(request, *args, **kwargs)


def partial_update(self, request, *args, **kwargs):  # pragma: no cover
    """
    Shortcut method.
    """
    return self.partial_update(request, *args, **kwargs)


CreateAPIView = type('CreateAPIView',
                     tuple(ADDITIONAL_MIXINS + [ChainingCreateUpdateMixin, mixins.CreateModelMixin, GenericAPIView]),
                     {'post': create,
                      '__doc__': "Concrete view for retrieving or deleting a model instance."})

ListAPIView = type('ListAPIView',
                   tuple(ADDITIONAL_MIXINS + [mixins.ListModelMixin, GenericAPIView]),
                   {'get': list_,
                    '__doc__': "Concrete view for listing a queryset."})


RetrieveAPIView = type('RetrieveAPIView',
                       tuple(ADDITIONAL_MIXINS + [mixins.RetrieveModelMixin, GenericAPIView]),
                       {'get': retrieve,
                        '__doc__': "Concrete view for retrieving a model instance."})


DestroyAPIView = type('DestroyAPIView',
                      tuple(ADDITIONAL_MIXINS + [mixins.DestroyModelMixin, GenericAPIView]),
                      {'delete': destroy,
                       '__doc__': "Concrete view for deleting a model instance."})


UpdateAPIView = type('UpdateAPIView',
                     tuple(ADDITIONAL_MIXINS + [ChainingCreateUpdateMixin, mixins.UpdateModelMixin, GenericAPIView]),
                     {'put': update,
                      'patch': partial_update,
                      '__doc__': "Concrete view for updating a model instance."})


ListCreateAPIView = type('ListCreateAPIView',
                         tuple(ADDITIONAL_MIXINS +
                               [ChainingCreateUpdateMixin, mixins.ListModelMixin,
                                mixins.CreateModelMixin, GenericAPIView]),
                         {'get': list_,
                          'post': create,
                          '__doc__': "Concrete view for listing a queryset or creating a model instance."})


RetrieveUpdateAPIView = type('RetrieveUpdateAPIView',
                             tuple(ADDITIONAL_MIXINS +
                                   [ChainingCreateUpdateMixin, mixins.RetrieveModelMixin,
                                    mixins.UpdateModelMixin, GenericAPIView]),
                             {'get': retrieve,
                              'put': update,
                              'patch': partial_update,
                              '__doc__': "Concrete view for retrieving, updating a model instance."})


RetrieveDestroyAPIView = type('RetrieveDestroyAPIView',
                              tuple(ADDITIONAL_MIXINS +
                                    [mixins.RetrieveModelMixin, mixins.DestroyModelMixin, GenericAPIView]),
                              {'get': retrieve,
                               'delete': destroy,
                               '__doc__': "Concrete view for retrieving or deleting a model instance."})


RetrieveUpdateDestroyAPIView = type('RetrieveUpdateDestroyAPIView',
                                    tuple(ADDITIONAL_MIXINS +
                                          [ChainingCreateUpdateMixin, mixins.RetrieveModelMixin,
                                           mixins.UpdateModelMixin, mixins.DestroyModelMixin, GenericAPIView]),
                                    {'get': retrieve,
                                     'put': update,
                                     'patch': partial_update,
                                     'delete': destroy,
                                     '__doc__': "Concrete view for retrieving, updating or deleting a model instance."
                                    })


class GenericViewSet(ViewSetMixin, GenericAPIView):  # pragma: no cover
    """
    The GenericViewSet class does not provide any actions by default,
    but does include the base set of generic view behavior, such as
    the `get_object` and `get_queryset` methods.
    """
    pass


ReadOnlyModelViewSet = type('ReadOnlyModelViewSet',
                            tuple(ADDITIONAL_MIXINS +
                                  [mixins.RetrieveModelMixin, mixins.ListModelMixin, GenericViewSet]),
                            {'__doc__': "A viewset that provides default `list()` and `retrieve()` actions."})


ModelViewSet = type('ModelViewSet',
                    tuple(ADDITIONAL_MIXINS +
                          [ChainingCreateUpdateMixin, mixins.CreateModelMixin, mixins.RetrieveModelMixin,
                           mixins.UpdateModelMixin, mixins.DestroyModelMixin, mixins.ListModelMixin, GenericViewSet]),
                    {'__doc__': "A viewset that provides default `create()`, `retrieve()`, `update()`, "
                                "`partial_update()`, `destroy()` and `list()` actions."})
