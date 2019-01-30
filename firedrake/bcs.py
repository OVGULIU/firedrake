# A module implementing strong (Dirichlet) boundary conditions.
import ufl
from ufl import as_ufl, SpatialCoordinate, UFLException
from ufl.algorithms.analysis import has_type
import finat

import pyop2 as op2
from pyop2.profiling import timed_function
from pyop2 import exceptions
from pyop2.utils import as_tuple

import firedrake.expression as expression
import firedrake.function as function
import firedrake.matrix as matrix
import firedrake.projection as projection
import firedrake.utils as utils
from firedrake import ufl_expr
from firedrake import slate


__all__ = ['DirichletBC', 'homogenize', 'EquationBC', 'EquationBCSplit']


class BCBase(object):
    r'''Implementation of a base class of Dirichlet-like boundary conditions.

    :arg V: the :class:`.FunctionSpace` on which the boundary condition
        should be applied.
    :arg sub_domain: the integer id(s) of the boundary region over which the
        boundary condition should be applied. The string "on_boundary" may be used
        to indicate all of the boundaries of the domain. In the case of extrusion
        the ``top`` and ``bottom`` strings are used to flag the bcs application on
        the top and bottom boundaries of the extruded mesh respectively.
    :arg method: the method for determining boundary nodes. The default is
        "topological", indicating that nodes topologically associated with a
        boundary facet will be included. The alternative value is "geometric",
        which indicates that nodes associated with basis functions which do not
        vanish on the boundary will be included. This can be used to impose
        strong boundary conditions on DG spaces, or no-slip conditions on HDiv spaces.
    '''
    def __init__(self, V, sub_domain, method="topological"):

        # First, we bail out on zany elements.  We don't know how to do BC's for them.
        if isinstance(V.finat_element, (finat.Argyris, finat.Morley, finat.Bell)) or \
           (isinstance(V.finat_element, finat.Hermite) and V.mesh().topological_dimension() > 1):
            raise NotImplementedError("Strong BCs not implemented for element %r, use Nitsche-type methods until we figure this out" % V.finat_element)
        self._function_space = V
        self.comm = V.comm
        self.sub_domain = sub_domain
        if method not in ["topological", "geometric"]:
            raise ValueError("Unknown boundary condition method %s" % method)
        self.method = method
        if V.extruded and V.component is not None:
            raise NotImplementedError("Indexed VFS bcs not implemented on extruded meshes")
        # If this BC is defined on a subspace (IndexedFunctionSpace or
        # ComponentFunctionSpace, possibly recursively), pull out the appropriate
        # indices.
        components = []
        indexing = []
        indices = []
        fs = self._function_space
        while True:
            # Add index to indices if found
            if fs.index is not None:
                indexing.append(fs.index)
                indices.append(fs.index)
            if fs.component is not None:
                components.append(fs.component)
                indices.append(fs.component)
            # Now try the parent
            if fs.parent is not None:
                fs = fs.parent
            else:
                # All done
                break
        # Used for indexing functions passed in.
        self._indices = tuple(reversed(indices))
        # Used for finding local to global maps with boundary conditions applied
        self._cache_key = (self.domain_args + (self.method, ) + tuple(indexing), tuple(components))

    def function_space(self):
        '''The :class:`.FunctionSpace` on which this boundary condition should
        be applied.'''

        return self._function_space

    @utils.cached_property
    def domain_args(self):
        r"""The sub_domain the BC applies to."""
        if isinstance(self.sub_domain, str):
            return (self.sub_domain, )
        return (as_tuple(self.sub_domain), )

    @utils.cached_property
    def nodes(self):
        '''The list of nodes at which this boundary condition applies.'''
        bcnodes = self._function_space.boundary_nodes(self.sub_domain, self.method)
        if isinstance(self._function_space.finat_element, finat.Hermite) and \
           self._function_space.mesh().topological_dimension() == 1:
            bcnodes = bcnodes[::2]  # every second dof is the vertex value
        return bcnodes

    @utils.cached_property
    def node_set(self):
        '''The subset corresponding to the nodes at which this
        boundary condition applies.'''

        return op2.Subset(self._function_space.node_set, self.nodes)

    def zero(self, r):
        r"""Zero the boundary condition nodes on ``r``.

        :arg r: a :class:`.Function` to which the
            boundary condition should be applied.

        """
        if isinstance(r, matrix.MatrixBase):
            raise NotImplementedError("Zeroing bcs on a Matrix is not supported")

        for idx in self._indices:
            r = r.sub(idx)
        try:
            r.dat.zero(subset=self.node_set)
        except exceptions.MapValueError:
            raise RuntimeError("%r defined on incompatible FunctionSpace!" % r)

    def integrals(self):
        raise NotImplementedError("integrals() method has to be overwritten")


class DirichletBC(BCBase):
    r'''Implementation of a strong Dirichlet boundary condition.

    :arg V: the :class:`.FunctionSpace` on which the boundary condition
        should be applied.
    :arg g: the boundary condition values. This can be a :class:`.Function` on
        ``V``, a :class:`.Constant`, an :class:`.Expression`, an
        iterable of literal constants (converted to an
        :class:`.Expression`), or a literal constant which can be
        pointwise evaluated at the nodes of
        ``V``. :class:`.Expression`\s are projected onto ``V`` if it
        does not support pointwise evaluation.
    :arg sub_domain: the integer id(s) of the boundary region over which the
        boundary condition should be applied. The string "on_boundary" may be used
        to indicate all of the boundaries of the domain. In the case of extrusion
        the ``top`` and ``bottom`` strings are used to flag the bcs application on
        the top and bottom boundaries of the extruded mesh respectively.
    :arg method: the method for determining boundary nodes. The default is
        "topological", indicating that nodes topologically associated with a
        boundary facet will be included. The alternative value is "geometric",
        which indicates that nodes associated with basis functions which do not
        vanish on the boundary will be included. This can be used to impose
        strong boundary conditions on DG spaces, or no-slip conditions on HDiv spaces.
    '''

    def __init__(self, V, g, sub_domain, method="topological"):
        super().__init__(V, sub_domain, method="topological")
        # Save the original value the user passed in.  If the user
        # passed in an Expression that has user-defined variables in
        # it, we need to remember it so that we can re-interpolate it
        # onto the function_arg if its state has changed.  Note that
        # the function_arg assignment is actually a property setter
        # which in the case of expressions interpolates it onto a
        # function and then throws the expression away.
        self._original_val = g
        self.function_arg = g
        self._original_arg = self.function_arg
        self._currently_zeroed = False

    @property
    def function_arg(self):
        '''The value of this boundary condition.'''
        if isinstance(self._original_val, expression.Expression):
            if not self._currently_zeroed and \
               self._original_val._state != self._expression_state:
                # Expression values have changed, need to reinterpolate
                self.function_arg = self._original_val
                # Remember "new" value of original arg, to work with zero/restore pair.
                self._original_arg = self.function_arg
        return self._function_arg

    def reconstruct(self, *, V=None, g=None, sub_domain=None, method=None):
        if V is None:
            V = self.function_space()
        if g is None:
            g = self._original_arg
        if sub_domain is None:
            sub_domain = self.sub_domain
        if method is None:
            method = self.method
        if V == self.function_space() and g == self._original_arg and \
           sub_domain == self.sub_domain and method == self.method:
            return self
        return type(self)(V, g, sub_domain, method=method)

    @function_arg.setter
    def function_arg(self, g):
        '''Set the value of this boundary condition.'''
        if isinstance(g, function.Function) and g.function_space() != self._function_space:
            raise RuntimeError("%r is defined on incompatible FunctionSpace!" % g)
        if not isinstance(g, expression.Expression):
            try:
                # Bare constant?
                as_ufl(g)
            except UFLException:
                try:
                    # List of bare constants? Convert to Expression
                    g = expression.to_expression(g)
                except ValueError:
                    raise ValueError("%r is not a valid DirichletBC expression" % (g,))
        if isinstance(g, expression.Expression) or has_type(as_ufl(g), SpatialCoordinate):
            if isinstance(g, expression.Expression):
                self._expression_state = g._state
            try:
                g = function.Function(self._function_space).interpolate(g)
            # Not a point evaluation space, need to project onto V
            except NotImplementedError:
                g = projection.project(g, self._function_space)
        self._function_arg = g
        self._currently_zeroed = False

    def homogenize(self):
        '''Convert this boundary condition into a homogeneous one.

        Set the value to zero.

        '''
        self.function_arg = 0
        self._currently_zeroed = True

    def restore(self):
        '''Restore the original value of this boundary condition.

        This uses the value passed on instantiation of the object.'''
        self._function_arg = self._original_arg
        self._currently_zeroed = False

    def set_value(self, val):
        '''Set the value of this boundary condition.

        :arg val: The boundary condition values.  See
            :class:`.DirichletBC` for valid values.
        '''
        self.function_arg = val
        self._original_arg = self.function_arg
        self._original_val = val

    @timed_function('ApplyBC')
    def apply(self, r, u=None):
        r"""Apply this boundary condition to ``r``.

        :arg r: a :class:`.Function` or :class:`.Matrix` to which the
            boundary condition should be applied.

        :arg u: an optional current state.  If ``u`` is supplied then
            ``r`` is taken to be a residual and the boundary condition
            nodes are set to the value ``u-bc``.  Supplying ``u`` has
            no effect if ``r`` is a :class:`.Matrix` rather than a
            :class:`.Function`. If ``u`` is absent, then the boundary
            condition nodes of ``r`` are set to the boundary condition
            values.


        If ``r`` is a :class:`.Matrix`, it will be assembled with a 1
        on diagonals where the boundary condition applies and 0 in the
        corresponding rows and columns.

        """

        if isinstance(r, matrix.MatrixBase):
            r.add_bc(self)
            return
        fs = self._function_space

        # Check that u matches r if supplied
        if u and u.function_space() != r.function_space():
            raise RuntimeError("Mismatching spaces for %s and %s" % (r, u))

        # Check that r's function space matches the BC's function
        # space. Run up through parents (IndexedFunctionSpace or
        # ComponentFunctionSpace) until we either match the function space, or
        # else we have a mismatch and raise an error.
        while True:
            if fs == r.function_space():
                break
            elif fs.parent is not None:
                fs = fs.parent
            else:
                raise RuntimeError("%r defined on incompatible FunctionSpace!" % r)

        # Apply the indexing to r (and u if supplied)
        for idx in self._indices:
            r = r.sub(idx)
            if u:
                u = u.sub(idx)
        if u:
            r.assign(u - self.function_arg, subset=self.node_set)
        else:
            r.assign(self.function_arg, subset=self.node_set)

    def set(self, r, val):
        r"""Set the boundary nodes to a prescribed (external) value.
        :arg r: the :class:`Function` to which the value should be applied.
        :arg val: the prescribed value.
        """
        for idx in self._indices:
            r = r.sub(idx)
            val = val.sub(idx)
        r.assign(val, subset=self.node_set)

    def integrals(self):
        return []


def homogenize(bc):
    r"""Create a homogeneous version of a :class:`.DirichletBC` object and return it. If
    ``bc`` is an iterable containing one or more :class:`.DirichletBC` objects,
    then return a list of the homogeneous versions of those :class:`.DirichletBC`\s.

    :arg bc: a :class:`.DirichletBC`, or iterable object comprising :class:`.DirichletBC`\(s).
    """
    try:
        return [homogenize(i) for i in bc]
    except TypeError:
        # not iterable
        return DirichletBC(bc.function_space(), 0, bc.sub_domain)


class EquationBC(BCBase):
    r'''Implementation of a form boundary condition.

    :arg V: the :class:`.FunctionSpace` on which the boundary condition
        should be applied.
    :arg g: the boundary condition values. This can be a :class:`.Function` on
        ``V``, a :class:`.Constant`, an :class:`.Expression`, an
        iterable of literal constants (converted to an
        :class:`.Expression`), or a literal constant which can be
        pointwise evaluated at the nodes of
        ``V``. :class:`.Expression`\s are projected onto ``V`` if it
        does not support pointwise evaluation.
    :arg sub_domain: the integer id(s) of the boundary region over which the
        boundary condition should be applied. The string "on_boundary" may be used
        to indicate all of the boundaries of the domain. In the case of extrusion
        the ``top`` and ``bottom`` strings are used to flag the bcs application on
        the top and bottom boundaries of the extruded mesh respectively.
    :arg method: the method for determining boundary nodes. The default is
        "topological", indicating that nodes topologically associated with a
        boundary facet will be included. The alternative value is "geometric",
        which indicates that nodes associated with basis functions which do not
        vanish on the boundary will be included. This can be used to impose
        strong boundary conditions on DG spaces, or no-slip conditions on HDiv spaces.
    '''

    def __init__(self, eq, u, sub_domain, J=None, Jp=None, method="topological"):
        super().__init__(eq.lhs.arguments()[0].function_space(), sub_domain, method="topological")
        self.u = u
        self.f = None
        self.bcs = None
        # This nested structure will enable recursive application of boundary conditions.
        #
        # def _assemble(..., bcs, ...)
        #     ...
        #     for bc in bcs:
        #         # boundary conditions for boundary conditions for boun...
        #         _assemble(..., bc.bcs, ...)
        #     ...

        self.Jp_eq_J = Jp is None

        #self.sub_domain = None
        # linear
        if isinstance(eq.lhs, ufl.Form) and isinstance(eq.rhs, ufl.Form):
            self.J = eq.lhs
            self.Jp = Jp or self.J
            if eq.rhs is 0:
                self.F = ufl_expr.action(self.J, self.u)
            else:
                if not isinstance(eq.rhs, (ufl.Form, slate.slate.TensorBase)):
                    raise TypeError("Provided BC RHS is a '%s', not a Form or Slate Tensor" % type(eq.rhs).__name__)
                if len(eq.rhs.arguments()) != 1:
                    raise ValueError("Provided BC RHS is not a linear form")
                self.F = ufl_expr.action(self.J, self.u) - eq.rhs
            self.is_linear = True
        # nonlinear
        else:
            if eq.rhs != 0:
                raise TypeError("RHS of a nonlinear form equation has to be 0")
            if not isinstance(eq.lhs, (ufl.Form, slate.slate.TensorBase)):
                raise TypeError("Provided BC residual is a '%s', not a Form or Slate Tensor" % type(eq.lhs).__name__)
            if len(eq.lhs.arguments()) != 1:
                raise ValueError("Provided BC residual is not a linear form")
            self.F = eq.lhs
            if J is not None and not isinstance(J, (ufl.Form, slate.slate.TensorBase)):
                raise TypeError("Provided BC Jacobian is a '%s', not a Form or Slate Tensor" % type(J).__name__)
            if J is not None and len(J.arguments()) != 2:
                raise ValueError("Provided BC Jacobian is not a bilinear form")
            self.J = J or ufl_expr.derivative(self.F, self.u)
            if Jp is not None and not isinstance(Jp, (ufl.Form, slate.slate.TensorBase)):
                raise TypeError("Provided BC preconditioner is a '%s', not a Form or Slate Tensor" % type(Jp).__name__)
            if Jp is not None and len(Jp.arguments()) != 2:
                raise ValueError("Provided BC preconditioner is not a bilinear form")
            self.Jp = Jp or self.J
            self.is_linear = False


class EquationBCSplit(BCBase):
    def __init__(self, ebc, form_str):
        if not isinstance(ebc, EquationBC):
            raise TypeError("Expected an instance of EquationBC")
        super(EquationBCSplit, self).__init__(ebc._function_space, ebc.sub_domain, method="topological")
        self.u = ebc.u
        if form_str == 'F':
            self.f = ebc.F
        elif form_str == 'J':
            self.f = ebc.J
        elif form_str == 'Jp':
            self.f = ebc.Jp
        else:
            raise TypeError("Undefined form type provided by form_str")

        self.bcs = None

    def integrals(self):
        return self.f.integrals()
