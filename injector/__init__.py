# encoding: utf-8
#
# Copyright (C) 2010 Alec Thomas <alec@swapoff.org>
# All rights reserved.
#
# This software is licensed as described in the file COPYING, which
# you should have received as part of this distribution.
#
# Author: Alec Thomas <alec@swapoff.org>

"""Injector - Python dependency injection framework, inspired by Guice

:copyright: (c) 2012 by Alec Thomas
:license: BSD
"""

import functools
import inspect
import itertools
import logging
import sys
import threading
import types
from abc import ABCMeta, abstractmethod
from collections import namedtuple
from typing import Any, Callable, cast, Dict, Generic, List, overload, Tuple, Type, TypeVar, Union

HAVE_ANNOTATED = sys.version_info >= (3, 7, 0)

if HAVE_ANNOTATED:
    # Ignoring errors here as typing_extensions stub doesn't know about those things yet
    from typing_extensions import _AnnotatedAlias, Annotated, get_type_hints  # type: ignore
else:
    Annotated = None
    from typing import get_type_hints as _get_type_hints

    def get_type_hints(what, include_extras):
        return _get_type_hints(what)


TYPING353 = hasattr(Union[str, int], '__origin__')


__author__ = 'Alec Thomas <alec@swapoff.org>'
__version__ = '0.17.0'
__version_tag__ = ''

log = logging.getLogger('injector')
log.addHandler(logging.NullHandler())

if log.level == logging.NOTSET:
    log.setLevel(logging.WARN)

T = TypeVar('T')
K = TypeVar('K')
V = TypeVar('V')


def private(something: T) -> T:
    something.__private__ = True  # type: ignore
    return something


CallableT = TypeVar('CallableT', bound=Callable)


def synchronized(lock: threading.RLock) -> Callable[[CallableT], CallableT]:
    def outside_wrapper(function: CallableT) -> CallableT:
        @functools.wraps(function)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            with lock:
                return function(*args, **kwargs)

        return cast(CallableT, wrapper)

    return outside_wrapper


lock = threading.RLock()


_inject_marker = object()
_noinject_marker = object()

if HAVE_ANNOTATED:
    InjectT = TypeVar('InjectT')
    Inject = Annotated[InjectT, _inject_marker]
    """An experimental way to declare injectable dependencies utilizing a `PEP 593`_ implementation
    in `typing_extensions`. This API is likely to change when `PEP 593`_ stabilizes.

    Those two declarations are equivalent::

        @inject
        def fun(t: SomeType) -> None:
            pass

        def fun(t: Inject[SomeType]) -> None:
            pass

    The advantage over using :func:`inject` is that if you have some noninjectable parameters
    it may be easier to spot what are they. Those two are equivalent::

        @inject
        @noninjectable('s')
        def fun(t: SomeType, s: SomeOtherType) -> None:
            pass

        def fun(t: Inject[SomeType], s: SomeOtherType) -> None:
            pass

    .. seealso::

        Function :func:`get_bindings`
            A way to inspect how various injection declarations interact with each other.

    .. versionadded:: 0.18.0
    .. note:: Requires Python 3.7+.

    .. _PEP 593: https://www.python.org/dev/peps/pep-0593/
    .. _typing_extensions: https://pypi.org/project/typing-extensions/
    """

    NoInject = Annotated[InjectT, _noinject_marker]
    """An experimental way to declare noninjectable dependencies utilizing a `PEP 593`_ implementation
    in `typing_extensions`. This API is likely to change when `PEP 593`_ stabilizes.

    Since :func:`inject` declares all function's parameters to be injectable there needs to be a way
    to opt out of it. This has been provided by :func:`noninjectable` but `noninjectable` suffers from
    two issues:

    * You need to repeat the parameter name
    * The declaration may be relatively distance in space from the actual parameter declaration, thus
      hindering readability

    `NoInject` solves both of those concerns, for example (those two declarations are equivalent)::

        @inject
        @noninjectable('b')
        def fun(a: TypeA, b: TypeB) -> None:
            pass

        @inject
        def fun(a: TypeA, b: NoInject[TypeB]) -> None:
            pass

    .. seealso::

        Function :func:`get_bindings`
            A way to inspect how various injection declarations interact with each other.

    .. versionadded:: 0.18.0
    .. note:: Requires Python 3.7+.

    .. _PEP 593: https://www.python.org/dev/peps/pep-0593/
    .. _typing_extensions: https://pypi.org/project/typing-extensions/
    """


def reraise(original: Exception, exception: Exception, maximum_frames: int = 1) -> None:
    prev_cls, prev, tb = sys.exc_info()
    frames = inspect.getinnerframes(cast(types.TracebackType, tb))
    if len(frames) > maximum_frames:
        exception = original
    raise exception.with_traceback(tb)


class Error(Exception):
    """Base exception."""


class UnsatisfiedRequirement(Error):
    """Requirement could not be satisfied."""

    def __str__(self):
        on = '%s has an ' % _describe(self.args[0]) if self.args[0] else ''
        return '%sunsatisfied requirement on %s' % (on, _describe(self.args[1].interface))


class CallError(Error):
    """Call to callable object fails."""

    def __str__(self):
        if len(self.args) == 1:
            return self.args[0]

        instance, method, args, kwargs, original_error, stack = self.args
        if hasattr(method, 'im_class'):
            instance = method.__self__
            method_name = method.__func__.__name__
        else:
            method_name = method.__name__

        cls = instance.__class__.__name__ if instance is not None else ''

        full_method = '.'.join((cls, method_name)).strip('.')

        parameters = ', '.join(
            itertools.chain(
                (repr(arg) for arg in args), ('%s=%r' % (key, value) for (key, value) in kwargs.items())
            )
        )
        return 'Call to %s(%s) failed: %s (injection stack: %r)' % (
            full_method,
            parameters,
            original_error,
            [level[0] for level in stack],
        )


class CircularDependency(Error):
    """Circular dependency detected."""


class UnknownProvider(Error):
    """Tried to bind to a type whose provider couldn't be determined."""


class UnknownArgument(Error):
    """Tried to mark an unknown argument as noninjectable."""


class Provider(Generic[T]):
    """Provides class instances."""

    __metaclass__ = ABCMeta

    @abstractmethod
    def get(self, injector: 'Injector') -> T:
        raise NotImplementedError  # pragma: no cover


class ClassProvider(Provider):
    """Provides instances from a given class, created using an Injector."""

    def __init__(self, cls: Type[T]) -> None:
        self._cls = cls

    def get(self, injector: 'Injector') -> T:
        return injector.create_object(self._cls)


class CallableProvider(Provider):
    """Provides something using a callable.

    The callable is called every time new value is requested from the provider.

    ::

        >>> Key = NewType('Key', int)
        >>> def factory():
        ...     print('providing')
        ...     return 333
        ...
        >>> def configure(binder):
        ...     binder.bind(Key, to=CallableProvider(factory))
        ...
        >>> injector = Injector(configure)
        >>> injector.get(key) is injector.get(key)
        providing
        providing
        False
        """

    def __init__(self, callable: Callable[..., T]):
        self._callable = callable

    def get(self, injector: 'Injector') -> T:
        return injector.call_with_injection(self._callable)

    def __repr__(self) -> str:
        return '%s(%r)' % (type(self).__name__, self._callable)


class InstanceProvider(Provider):
    """Provide a specific instance.

    ::

        >>> class MyType:
        ...     def __init__(self):
        ...         self.contents = []
        >>> def configure(binder):
        ...     binder.multibind(MyType, to=InstanceProvider(MyType()))
        ...
        >>> injector = Injector(configure)
        >>> injector.get(MyType) is injector.get(MyType)
        True
        >>> injector.get(MyType).contents.append('x')
        >>> injector.get(MyType).contents
        ['x']
    """

    def __init__(self, instance: T) -> None:
        self._instance = instance

    def get(self, injector: 'Injector') -> T:
        return self._instance

    def __repr__(self) -> str:
        return '%s(%r)' % (type(self).__name__, self._instance)


@private
class ListOfProviders(Provider[List[T]]):
    """Provide a list of instances via other Providers."""

    def __init__(self) -> None:
        self._providers = []  # type: List[Provider[T]]

    def append(self, provider: Provider[T]) -> None:
        self._providers.append(provider)

    def get(self, injector: 'Injector') -> List[T]:
        return [provider.get(injector) for provider in self._providers]

    def __repr__(self) -> str:
        return '%s(%r)' % (type(self).__name__, self._providers)


class MultiBindProvider(ListOfProviders):
    """Used by :meth:`Binder.multibind` to flatten results of providers that
    return sequences."""

    def get(self, injector: 'Injector') -> List[T]:
        return [i for provider in self._providers for i in provider.get(injector)]


class MapBindProvider(ListOfProviders):
    """A provider for map bindings."""

    def get(self, injector):
        map = {}
        for provider in self._providers:
            map.update(provider.get(injector))
        return map


_BindingBase = namedtuple('_BindingBase', 'interface provider scope')


@private
class Binding(_BindingBase):
    """A binding from an (interface,) to a provider in a scope."""

    def is_multibinding(self) -> bool:
        return _get_origin(_punch_through_alias(self.interface)) in {dict, list}


class Binder:
    """Bind interfaces to implementations.

    .. note:: This class is instantiated internally for you and there's no need
        to instantiate it on your own.
    """

    @private
    def __init__(self, injector: 'Injector', auto_bind: bool = True, parent: 'Binder' = None) -> None:
        """Create a new Binder.

        :param injector: Injector we are binding for.
        :param auto_bind: Whether to automatically bind missing types.
        :param parent: Parent binder.
        """
        self.injector = injector
        self._auto_bind = auto_bind
        self._bindings = {}  # type: Dict[type, Binding]
        self.parent = parent

    def bind(
        self,
        interface: Type[T],
        to: Union[None, T, Callable[..., T], Provider[T]] = None,
        scope: Union[None, Type['Scope'], 'ScopeDecorator'] = None,
    ) -> None:
        """Bind an interface to an implementation.

        `typing.List` and `typing.Dict` instances are reserved for multibindings and trying to bind them
        here will result in an error (use :meth:`multibind` instead)::

            binder.bind(List[str], to=['hello', 'there'])  # Error

        :param interface: Type to bind.
        :param to: Instance or class to bind to, or an explicit
             :class:`Provider` subclass.
        :param scope: Optional :class:`Scope` in which to bind.
        """
        if _get_origin(_punch_through_alias(interface)) in {dict, list}:
            raise Error(
                'Type %s is reserved for multibindings. Use multibind instead of bind.' % (interface,)
            )
        self._bindings[interface] = self.create_binding(interface, to, scope)

    @overload
    def multibind(
        self,
        interface: Type[List[T]],
        to: Union[List[T], Callable[..., List[T]], Provider[List[T]]],
        scope: Union[Type['Scope'], 'ScopeDecorator'] = None,
    ) -> None:
        pass

    @overload
    def multibind(
        self,
        interface: Type[Dict[K, V]],
        to: Union[Dict[K, V], Callable[..., Dict[K, V]], Provider[Dict[K, V]]],
        scope: Union[Type['Scope'], 'ScopeDecorator'] = None,
    ) -> None:
        pass

    def multibind(self, interface, to, scope=None):
        """Creates or extends a multi-binding.

        A multi-binding contributes values to a list or to a dictionary. For example::

            binder.multibind(List[str], to=['some', 'strings'])
            binder.multibind(List[str], to=['other', 'strings'])
            injector.get(List[str])  # ['some', 'strings', 'other', 'strings']

            binder.multibind(Dict[str, int], to={'key': 11})
            binder.multibind(Dict[str, int], to={'other_key': 33})
            injector.get(Dict[str, int])  # {'key': 11, 'other_key': 33}

        .. versionchanged:: 0.17.0
            Added support for using `typing.Dict` and `typing.List` instances as interfaces.
            Deprecated support for `MappingKey`, `SequenceKey` and single-item lists and
            dictionaries as interfaces.

        :param interface: typing.Dict or typing.List instance to bind to.
        :param to: Instance, class to bind to, or an explicit :class:`Provider`
                subclass. Must provide a list or a dictionary, depending on the interface.
        :param scope: Optional Scope in which to bind.
        """
        if interface not in self._bindings:
            if (
                isinstance(interface, dict)
                or isinstance(interface, type)
                and issubclass(interface, dict)
                or _get_origin(_punch_through_alias(interface)) is dict
            ):
                provider = MapBindProvider()
            else:
                provider = MultiBindProvider()
            binding = self.create_binding(interface, provider, scope)
            self._bindings[interface] = binding
        else:
            binding = self._bindings[interface]
            provider = binding.provider
            assert isinstance(provider, ListOfProviders)
        provider.append(self.provider_for(interface, to))

    def install(self, module: Union[Callable[['Binder'], None], 'Module', Type['Module']]) -> None:
        """Install a module into this binder.

        In this context the module is one of the following:

        * function taking the :class:`Binder` as it's only parameter

          ::

            def configure(binder):
                bind(str, to='s')

            binder.install(configure)

        * instance of :class:`Module` (instance of it's subclass counts)

          ::

            class MyModule(Module):
                def configure(self, binder):
                    binder.bind(str, to='s')

            binder.install(MyModule())

        * subclass of :class:`Module` - the subclass needs to be instantiable so if it
          expects any parameters they need to be injected

          ::

            binder.install(MyModule)
        """
        if type(module) is type and issubclass(cast(type, module), Module):
            instance = cast(type, module)()
        else:
            instance = module
        instance(self)

    def create_binding(self, interface, to=None, scope=None):
        provider = self.provider_for(interface, to)
        scope = scope or getattr(to or interface, '__scope__', NoScope)
        if isinstance(scope, ScopeDecorator):
            scope = scope.scope
        return Binding(interface, provider, scope)

    def provider_for(self, interface, to=None):
        base_type = _punch_through_alias(interface)
        origin = _get_origin(base_type)

        if interface is Any:
            raise TypeError('Injecting Any is not supported')
        elif _is_specialization(interface, ProviderOf):
            (target,) = interface.__args__
            if to is not None:
                raise Exception('ProviderOf cannot be bound to anything')
            return InstanceProvider(ProviderOf(self.injector, target))
        elif isinstance(to, Provider):
            return to
        elif isinstance(
            to,
            (
                types.FunctionType,
                types.LambdaType,
                types.MethodType,
                types.BuiltinFunctionType,
                types.BuiltinMethodType,
            ),
        ):
            return CallableProvider(to)
        elif issubclass(type(to), type):
            return ClassProvider(to)
        elif isinstance(interface, BoundKey):

            def proxy(**kwargs):
                return interface.interface(**kwargs)

            proxy.__annotations__ = interface.kwargs.copy()
            return CallableProvider(inject(proxy))
        elif _is_specialization(interface, AssistedBuilder):
            (target,) = interface.__args__
            builder = interface(self.injector, target)
            return InstanceProvider(builder)
        elif (
            origin is None
            and isinstance(base_type, (tuple, type))
            and interface is not Any
            and isinstance(to, base_type)
            or origin in {dict, list}
            and isinstance(to, origin)
        ):
            return InstanceProvider(to)
        elif issubclass(type(base_type), type) or isinstance(base_type, (tuple, list)):
            if to is not None:
                return InstanceProvider(to)
            return ClassProvider(base_type)

        else:
            raise UnknownProvider('couldn\'t determine provider for %r to %r' % (interface, to))

    def _get_binding(self, key, *, only_this_binder: bool = False):
        binding = self._bindings.get(key)
        if binding:
            return binding, self
        if self.parent and not only_this_binder:
            return self.parent._get_binding(key)

        raise KeyError

    def get_binding(self, interface):
        is_scope = isinstance(interface, type) and issubclass(interface, Scope)
        try:
            return self._get_binding(interface, only_this_binder=is_scope)
        except (KeyError, UnsatisfiedRequirement):
            if is_scope:
                scope = interface
                self.bind(scope, to=scope(self.injector))
                return self._get_binding(interface)
            # The special interface is added here so that requesting a special
            # interface with auto_bind disabled works
            if self._auto_bind or self._is_special_interface(interface):
                binding = self.create_binding(interface)
                self._bindings[interface] = binding
                return binding, self

        raise UnsatisfiedRequirement(interface)

    def _is_special_interface(self, interface):
        # "Special" interfaces are ones that you cannot bind yourself but
        # you can request them (for example you cannot bind ProviderOf(SomeClass)
        # to anything but you can inject ProviderOf(SomeClass) just fine
        return any(_is_specialization(interface, cls) for cls in [AssistedBuilder, ProviderOf])


if TYPING353:

    def _is_specialization(cls, generic_class):
        # Starting with typing 3.5.3/Python 3.6 it is no longer necessarily true that
        # issubclass(SomeGeneric[X], SomeGeneric) so we need some other way to
        # determine whether a particular object is a generic class with type parameters
        # provided. Fortunately there seems to be __origin__ attribute that's useful here.

        # We need to special-case Annotated as its __origin__ behaves differently than
        # other typing generic classes. See https://github.com/python/typing/pull/635
        # for some details.
        if HAVE_ANNOTATED and generic_class is Annotated and isinstance(cls, _AnnotatedAlias):
            return True

        if not hasattr(cls, '__origin__'):
            return False
        origin = cls.__origin__
        if not inspect.isclass(generic_class):
            generic_class = type(generic_class)
        if not inspect.isclass(origin):
            origin = type(origin)
        # __origin__ is generic_class is a special case to handle Union as
        # Union cannot be used in issubclass() check (it raises an exception
        # by design).
        return origin is generic_class or issubclass(origin, generic_class)


else:
    # To maintain compatibility we fall back to an issubclass check.
    def _is_specialization(cls, generic_class):
        return isinstance(cls, type) and cls is not Any and issubclass(cls, generic_class)


def _punch_through_alias(type_: Any) -> type:
    if getattr(type_, '__qualname__', '') == 'NewType.<locals>.new_type':
        return type_.__supertype__
    else:
        return type_


def _get_origin(type_: type) -> type:
    origin = getattr(type_, '__origin__', None)
    # Older typing behaves differently there and stores Dict and List as origin, we need to be flexible.
    if origin is List:
        return list
    elif origin is Dict:
        return dict
    return origin


class Scope:
    """A Scope looks up the Provider for a binding.

    By default (ie. :class:`NoScope` ) this simply returns the default
    :class:`Provider` .
    """

    __metaclass__ = ABCMeta

    def __init__(self, injector):
        self.injector = injector
        self.configure()

    def configure(self):
        """Configure the scope."""

    @abstractmethod
    def get(self, key, provider):
        """Get a :class:`Provider` for a key.

        :param key: The key to return a provider for.
        :param provider: The default Provider associated with the key.
        :returns: A Provider instance that can provide an instance of key.
        """
        raise NotImplementedError  # pragma: no cover


class ScopeDecorator:
    def __init__(self, scope):
        self.scope = scope

    def __call__(self, cls):
        cls.__scope__ = self.scope
        binding = getattr(cls, '__binding__', None)
        if binding:
            new_binding = Binding(interface=binding.interface, provider=binding.provider, scope=self.scope)
            setattr(cls, '__binding__', new_binding)
        return cls

    def __repr__(self):
        return 'ScopeDecorator(%s)' % self.scope.__name__


class NoScope(Scope):
    """An unscoped provider."""

    def get(self, unused_key, provider):
        return provider


noscope = ScopeDecorator(NoScope)


class SingletonScope(Scope):
    """A :class:`Scope` that returns a per-Injector instance for a key.

    :data:`singleton` can be used as a convenience class decorator.

    >>> class A: pass
    >>> injector = Injector()
    >>> provider = ClassProvider(A)
    >>> singleton = SingletonScope(injector)
    >>> a = singleton.get(A, provider)
    >>> b = singleton.get(A, provider)
    >>> a is b
    True
    """

    def configure(self):
        self._context = {}

    @synchronized(lock)
    def get(self, key, provider):
        try:
            return self._context[key]
        except KeyError:
            provider = InstanceProvider(provider.get(self.injector))
            self._context[key] = provider
            return provider


singleton = ScopeDecorator(SingletonScope)


class ThreadLocalScope(Scope):
    """A :class:`Scope` that returns a per-thread instance for a key."""

    def configure(self):
        self._locals = threading.local()

    def get(self, key, provider):
        try:
            return getattr(self._locals, repr(key))
        except AttributeError:
            provider = InstanceProvider(provider.get(self.injector))
            setattr(self._locals, repr(key), provider)
            return provider


threadlocal = ScopeDecorator(ThreadLocalScope)


class Module:
    """Configures injector and providers."""

    def __call__(self, binder):
        """Configure the binder."""
        self.__injector__ = binder.injector
        for unused_name, function in inspect.getmembers(self, inspect.ismethod):
            binding = None
            if hasattr(function, '__binding__'):
                binding = function.__binding__
                bind_method = binder.multibind if binding.is_multibinding() else binder.bind
                bind_method(
                    binding.interface, to=types.MethodType(binding.provider, self), scope=binding.scope
                )
        self.configure(binder)

    def configure(self, binder):
        """Override to configure bindings."""


class Injector:
    """
    :param modules: Optional - a configuration module or iterable of configuration modules.
        Each module will be installed in current :class:`Binder` using :meth:`Binder.install`.

        Consult :meth:`Binder.install` documentation for the details.

    :param auto_bind: Whether to automatically bind missing types.
    :param parent: Parent injector.

    .. versionadded:: 0.7.5
        ``use_annotations`` parameter

    .. versionchanged:: 0.13.0
        ``use_annotations`` parameter is removed
    """

    def __init__(self, modules=None, auto_bind=True, parent=None):
        # Stack of keys currently being injected. Used to detect circular
        # dependencies.
        self._stack = ()

        self.parent = parent

        # Binder
        self.binder = Binder(self, auto_bind=auto_bind, parent=parent and parent.binder)

        if not modules:
            modules = []
        elif not hasattr(modules, '__iter__'):
            modules = [modules]

        # Bind some useful types
        self.binder.bind(Injector, to=self)
        self.binder.bind(Binder, to=self.binder)
        # Initialise modules
        for module in modules:
            self.binder.install(module)

    @property
    def _log_prefix(self):
        return '>' * (len(self._stack) + 1) + ' '

    def get(self, interface: Type[T], scope=None) -> T:
        """Get an instance of the given interface.

        .. note::

            Although this method is part of :class:`Injector`'s public interface
            it's meant to be used in limited set of circumstances.

            For example, to create some kind of root object (application object)
            of your application (note that only one `get` call is needed,
            inside the `Application` class and any of its dependencies
            :func:`inject` can and should be used):

            .. code-block:: python

                class Application:

                    @inject
                    def __init__(self, dep1: Dep1, dep2: Dep2):
                        self.dep1 = dep1
                        self.dep2 = dep2

                    def run(self):
                        self.dep1.something()

                injector = Injector(configuration)
                application = injector.get(Application)
                application.run()

        :param interface: Interface whose implementation we want.
        :param scope: Class of the Scope in which to resolve.
        :returns: An implementation of interface.
        """
        binding, binder = self.binder.get_binding(interface)
        scope = scope or binding.scope
        if isinstance(scope, ScopeDecorator):
            scope = scope.scope
        # Fetch the corresponding Scope instance from the Binder.
        scope_binding, _ = binder.get_binding(scope)
        scope_instance = scope_binding.provider.get(self)

        log.debug(
            '%sInjector.get(%r, scope=%r) using %r', self._log_prefix, interface, scope, binding.provider
        )
        result = scope_instance.get(interface, binding.provider).get(self)
        log.debug('%s -> %r', self._log_prefix, result)
        return result

    def create_child_injector(self, *args, **kwargs):
        return Injector(*args, parent=self, **kwargs)

    def create_object(self, cls: Type[T], additional_kwargs=None) -> T:
        """Create a new instance, satisfying any dependencies on cls."""
        additional_kwargs = additional_kwargs or {}
        log.debug('%sCreating %r object with %r', self._log_prefix, cls, additional_kwargs)

        try:
            instance = cls.__new__(cls)
        except TypeError as e:
            reraise(
                e,
                CallError(cls, getattr(cls.__new__, '__func__', cls.__new__), (), {}, e, self._stack),
                maximum_frames=2,
            )
        try:
            init = cls.__init__
            self.call_with_injection(init, self_=instance, kwargs=additional_kwargs)
        except TypeError as e:
            reraise(e, CallError(instance, instance.__init__.__func__, (), additional_kwargs, e, self._stack))
        return instance

    def call_with_injection(self, callable, self_=None, args=(), kwargs={}):
        """Call a callable and provide it's dependencies if needed.

        :param self_: Instance of a class callable belongs to if it's a method,
            None otherwise.
        :param args: Arguments to pass to callable.
        :param kwargs: Keyword arguments to pass to callable.
        :type callable: callable
        :type args: tuple of objects
        :type kwargs: dict of string -> object
        :return: Value returned by callable.
        """

        bindings = get_bindings(callable)
        signature = inspect.signature(callable)
        full_args = args
        if self_ is not None:
            full_args = (self_,) + full_args
        bound_arguments = signature.bind_partial(*full_args)

        needed = dict(
            (k, v) for (k, v) in bindings.items() if k not in kwargs and k not in bound_arguments.arguments
        )

        dependencies = self.args_to_inject(
            function=callable,
            bindings=needed,
            owner_key=self_.__class__ if self_ is not None else callable.__module__,
        )

        dependencies.update(kwargs)

        try:
            return callable(*full_args, **dependencies)
        except TypeError as e:
            reraise(e, CallError(self_, callable, args, dependencies, e, self._stack))

    @private
    @synchronized(lock)
    def args_to_inject(self, function, bindings, owner_key):
        """Inject arguments into a function.

        :param function: The function.
        :param bindings: Map of argument name to binding key to inject.
        :param owner_key: A key uniquely identifying the *scope* of this function.
            For a method this will be the owning class.
        :returns: Dictionary of resolved arguments.
        """
        dependencies = {}

        key = (owner_key, function, tuple(sorted(bindings.items())))

        def repr_key(k):
            owner_key, function, bindings = k
            return '%s.%s(injecting %s)' % (tuple(map(_describe, k[:2])) + (dict(k[2]),))

        log.debug('%sProviding %r for %r', self._log_prefix, bindings, function)

        if key in self._stack:
            raise CircularDependency(
                'circular dependency detected: %s -> %s'
                % (' -> '.join(map(repr_key, self._stack)), repr_key(key))
            )

        self._stack += (key,)
        try:
            for arg, interface in bindings.items():
                try:
                    instance = self.get(interface)
                except UnsatisfiedRequirement as e:
                    if not e.args[0]:
                        e = UnsatisfiedRequirement(owner_key, e.args[1])
                    raise e
                dependencies[arg] = instance
        finally:
            self._stack = tuple(self._stack[:-1])

        return dependencies


def get_bindings(callable: Callable) -> Dict[str, type]:
    """Get bindings of injectable parameters from a callable.

    If the callable is not decorated with :func:`inject` and does not have any of its
    parameters declared as injectable using :data:`Inject` an empty dictionary will
    be returned.  Otherwise the returned dictionary will contain a mapping
    between parameter names and their types with the exception of parameters
    excluded from dependency injection (either with :func:`noninjectable`, :data:`NoInject`
    or only explicit injection with :data:`Inject` being used). For example::

        >>> def function1(a: int) -> None:
        ...     pass
        ...
        >>> get_bindings(function1)
        {}

        >>> @inject
        ... def function2(a: int) -> None:
        ...     pass
        ...
        >>> get_bindings(function2)
        {'a': int}

        >>> @inject
        ... @noninjectable('b')
        ... def function3(a: int, b: str) -> None:
        ...     pass
        ...
        >>> get_bindings(function3)
        {'a': int}

        >>> # The simple case of no @inject but injection requested with Inject[...]
        >>> def function4(a: Inject[int], b: str) -> None:
        ... pass
        ...
        >>> get_bindings(function4)
        {'a': int}

        >>> # Using @inject with Inject is redundant but it should not break anything
        >>> @inject
        ... def function5(a: Inject[int], b: str) -> None:
        ...     pass
        ...
        >>> get_bindings(function5)
        {'a': int, 'b': str}

        >>> # We need to be able to exclude a parameter from injection with NoInject
        >>> @inject
        ... def function6(a: int, b: NoInject[str]) -> None:
        ...     pass
        ...
        >>> get_bindings(function6)
        {'a': int}

        >>> # The presence of NoInject should not trigger anything on its own
        >>> def function7(a: int, b: NoInject[str]) -> None:
        ...     pass
        ...
        >>> get_bindings(function7)
        {}

    This function is used internally so by calling it you can learn what exactly
    Injector is going to try to provide to a callable.
    """
    look_for_explicit_bindings = False
    if not hasattr(callable, '__bindings__'):
        type_hints = get_type_hints(callable, include_extras=True)
        has_injectable_parameters = any(
            _is_specialization(v, Annotated) and _inject_marker in v.__metadata__ for v in type_hints.values()
        )

        if not has_injectable_parameters:
            return {}
        else:
            look_for_explicit_bindings = True

    if look_for_explicit_bindings or cast(Any, callable).__bindings__ == 'deferred':
        read_and_store_bindings(
            callable, _infer_injected_bindings(callable, only_explicit_bindings=look_for_explicit_bindings)
        )
    noninjectables = getattr(callable, '__noninjectables__', set())
    return {k: v for k, v in cast(Any, callable).__bindings__.items() if k not in noninjectables}


class _BindingNotYetAvailable(Exception):
    pass


def _infer_injected_bindings(callable, only_explicit_bindings: bool):
    spec = inspect.getfullargspec(callable)
    try:
        bindings = get_type_hints(callable, include_extras=True)
    except NameError as e:
        raise _BindingNotYetAvailable(e)

    # We don't care about the return value annotation as it doesn't matter
    # injection-wise.
    bindings.pop('return', None)

    # If we're dealing with a bound method get_type_hints will still return `self` annotation even though
    # it's already provided and we're not really interested in its type. So – drop it.
    if isinstance(callable, types.MethodType):
        self_name = spec.args[0]
        bindings.pop(self_name, None)

    # variadic arguments aren't supported at the moment (this may change
    # in the future if someone has a good idea how to utilize them)
    bindings.pop(spec.varargs, None)
    bindings.pop(spec.varkw, None)

    for k, v in list(bindings.items()):
        if _is_specialization(v, Annotated):
            v, metadata = v.__origin__, v.__metadata__
            bindings[k] = v
        else:
            metadata = tuple()

        if only_explicit_bindings and _inject_marker not in metadata or _noinject_marker in metadata:
            del bindings[k]
            break

        if _is_specialization(v, Union):
            # We don't treat Optional parameters in any special way at the moment.
            if TYPING353:
                union_members = v.__args__
            else:
                union_members = v.__union_params__
            new_members = tuple(set(union_members) - {type(None)})
            # mypy stared complaining about this line for some reason:
            #     error: Variable "new_members" is not valid as a type
            new_union = Union[new_members]  # type: ignore
            # mypy complains about this construct:
            #     error: The type alias is invalid in runtime context
            # See: https://github.com/python/mypy/issues/5354
            bindings[k] = new_union  # type: ignore

    return bindings


def provider(function):
    """Decorator for :class:`Module` methods, registering a provider of a type.

    >>> class MyModule(Module):
    ...   @provider
    ...   def provide_name(self) -> str:
    ...       return 'Bob'

    @provider-decoration implies @inject so you can omit it and things will
    work just the same:

    >>> class MyModule2(Module):
    ...     def configure(self, binder):
    ...         binder.bind(int, to=654)
    ...
    ...     @provider
    ...     def provide_str(self, i: int) -> str:
    ...         return str(i)
    ...
    >>> injector = Injector(MyModule2)
    >>> injector.get(str)
    '654'
    """
    _mark_provider_function(function, allow_multi=False)
    return function


def multiprovider(function: CallableT) -> CallableT:
    """Like :func:`provider`, but for multibindings. Example usage::

        class MyModule(Module):
            @multiprovider
            def provide_strs(self) -> List[str]:
                return ['str1']

        class OtherModule(Module):
            @multiprovider
            def provide_strs_also(self) -> List[str]:
                return ['str2']

        Injector([MyModule, OtherModule]).get(List[str])  # ['str1', 'str2']

    See also: :meth:`Binder.multibind`."""
    _mark_provider_function(function, allow_multi=True)
    return function


def _mark_provider_function(function: Callable, *, allow_multi: bool) -> None:
    scope_ = getattr(function, '__scope__', None)
    annotations = inspect.getfullargspec(function).annotations
    return_type = annotations['return']
    origin = _get_origin(_punch_through_alias(return_type))
    if origin in {dict, list} and not allow_multi:
        raise Error(
            'Function %s needs to be decorated with multiprovider instead of provider if it is to '
            'provide values to a multibinding of type %s' % (function.__name__, return_type)
        )
    binding = Binding(return_type, inject(function), scope_)
    function.__binding__ = binding  # type: ignore


def inject(constructor_or_class):
    """Decorator declaring parameters to be injected.

    eg.

    >>> class A:
    ...     @inject
    ...     def __init__(self, number: int, name: str):
    ...         print([number, name])
    ...
    >>> def configure(binder):
    ...     binder.bind(A)
    ...     binder.bind(int, to=123)
    ...     binder.bind(str, to='Bob')

    Use the Injector to get a new instance of A:

    >>> a = Injector(configure).get(A)
    [123, 'Bob']

    As a convenience one can decorate a class itself:

    >>> @inject
    ... class B:
    ...     def __init__(self, dependency: Dependency):
    ...         self.dependency = dependency

    This is equivalent to decorating its constructor. In particular this provides integration with
    `dataclasses <https://docs.python.org/3/library/dataclasses.html>`_ (the order of decorator
    application is important here):

    >>> @inject
    ... @dataclass
    ... class C:
    ...     dependency: Dependency

    .. note::

        This decorator is to be used on class constructors (or, as a convenience, on classes).
        Using it on non-constructor methods worked in the past but it was an implementation
        detail rather than a design decision.

        Third party libraries may, however, provide support for injecting dependencies
        into non-constructor methods or free functions in one form or another.

    .. seealso::

        Generic type :data:`Inject`
            A more explicit way to declare parameters as injectable.

        Function :func:`get_bindings`
            A way to inspect how various injection declarations interact with each other.

    .. versionchanged:: 0.16.2

        (Re)added support for decorating classes with @inject.
    """
    if isinstance(constructor_or_class, type) and hasattr(constructor_or_class, '__init__'):
        inject(constructor_or_class.__init__)
    else:
        function = constructor_or_class
        try:
            bindings = _infer_injected_bindings(function, only_explicit_bindings=False)
            read_and_store_bindings(function, bindings)
        except _BindingNotYetAvailable:
            function.__bindings__ = 'deferred'
    return constructor_or_class


def noninjectable(*args):
    """Mark some parameters as not injectable.

    This serves as documentation for people reading the code and will prevent
    Injector from ever attempting to provide the parameters.

    For example:

    >>> class Service:
    ...    pass
    ...
    >>> class SomeClass:
    ...     @inject
    ...     @noninjectable('user_id')
    ...     def __init__(self, service: Service, user_id: int):
    ...         # ...
    ...         pass

    :func:`noninjectable` decorations can be stacked on top of
    each other and the order in which a function is decorated with
    :func:`inject` and :func:`noninjectable`
    doesn't matter.

    .. seealso::

        Generic type :data:`NoInject`
            A nicer way to declare parameters as noninjectable.

        Function :func:`get_bindings`
            A way to inspect how various injection declarations interact with each other.

    """

    def decorator(function):
        argspec = inspect.getfullargspec(inspect.unwrap(function))
        for arg in args:
            if arg not in argspec.args and arg not in argspec.kwonlyargs:
                raise UnknownArgument('Unable to mark unknown argument %s ' 'as non-injectable.' % arg)

        existing = getattr(function, '__noninjectables__', set())
        merged = existing | set(args)
        function.__noninjectables__ = merged
        return function

    return decorator


@private
def read_and_store_bindings(f, bindings):
    function_bindings = getattr(f, '__bindings__', None) or {}
    if function_bindings == 'deferred':
        function_bindings = {}
    merged_bindings = dict(function_bindings, **bindings)

    if hasattr(f, '__func__'):
        f = f.__func__
    f.__bindings__ = merged_bindings


class BoundKey(tuple):
    """A BoundKey provides a key to a type with pre-injected arguments.

    >>> class A:
    ...   def __init__(self, a, b):
    ...     self.a = a
    ...     self.b = b
    >>> InjectedA = BoundKey(A, a=InstanceProvider(1), b=InstanceProvider(2))
    >>> injector = Injector()
    >>> a = injector.get(InjectedA)
    >>> a.a, a.b
    (1, 2)
    """

    def __new__(cls, interface: Type[T], **kwargs: Any) -> 'BoundKey':
        kwargs_tuple = tuple(sorted(kwargs.items()))
        return super(BoundKey, cls).__new__(cls, (interface, kwargs_tuple))  # type: ignore

    @property
    def interface(self) -> Type[T]:
        return self[0]

    @property
    def kwargs(self) -> Dict[str, Any]:
        return dict(self[1])


class AssistedBuilder(Generic[T]):
    def __init__(self, injector: Injector, target: Type[T]) -> None:
        self._injector = injector
        self._target = target

    def build(self, **kwargs: Any) -> T:
        binder = self._injector.binder
        binding, _ = binder.get_binding(self._target)
        provider = binding.provider
        if not isinstance(provider, ClassProvider):
            raise Error(
                'Assisted interface building works only with ClassProviders, '
                'got %r for %r' % (provider, binding.interface)
            )

        return self._build_class(cast(Type[T], provider._cls), **kwargs)

    def _build_class(self, cls: Type[T], **kwargs: Any) -> T:
        return self._injector.create_object(cls, additional_kwargs=kwargs)


class ClassAssistedBuilder(AssistedBuilder[T]):
    def build(self, **kwargs: Any) -> T:
        return self._build_class(self._target, **kwargs)


def _describe(c: Any) -> str:
    if hasattr(c, '__name__'):
        return cast(str, c.__name__)
    if type(c) in (tuple, list):
        return '[%s]' % c[0].__name__
    return str(c)


class ProviderOf(Generic[T]):
    """Can be used to get a provider of an interface, for example:

        >>> def provide_int():
        ...     print('providing')
        ...     return 123
        >>>
        >>> def configure(binder):
        ...     binder.bind(int, to=provide_int)
        >>>
        >>> injector = Injector(configure)
        >>> provider = injector.get(ProviderOf[int])
        >>> value = provider.get()
        providing
        >>> value
        123
    """

    def __init__(self, injector: Injector, interface: Type[T]):
        self._injector = injector
        self._interface = interface

    def __repr__(self) -> str:
        return '%s(%r, %r)' % (type(self).__name__, self._injector, self._interface)

    def get(self) -> T:
        """Get an implementation for the specified interface."""
        return self._injector.get(self._interface)


def is_decorated_with_inject(function: Callable[..., Any]) -> bool:
    """See if given callable is declared to want some dependencies injected.

    Example use:

    >>> def fun(i: int) -> str:
    ...     return str(i)

    >>> is_decorated_with_inject(fun)
    False
    >>>
    >>> @inject
    ... def fun2(i: int) -> str:
    ...     return str(i)

    >>> is_decorated_with_inject(fun2)
    True
    """
    return hasattr(function, '__bindings__')
