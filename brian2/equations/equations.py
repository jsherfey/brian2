'''
Differential equations for Brian models.
'''
import inspect
import keyword
import re
import string

from pyparsing import (Group, ZeroOrMore, OneOrMore, Optional, Word, CharsNotIn,
                       Combine, Suppress, restOfLine, LineEnd, ParseException)

from brian2.units.fundamentalunits import DimensionMismatchError, get_dimensions
from brian2.units.allunits import second
from brian2.equations.unitcheck import (get_unit_from_string,
                                        get_default_unit_namespace)
from brian2.utils.stringtools import word_substitute, get_identifiers

__all__ = ['Equations', 'CodeString']

# A dictionary mapping equation types to nice names for error messages
EQUATION_TYPE = {'parameter': 'parameter',
                 'diff_equation': 'differential equation',
                 'static_equation': 'static equation'}

# Units of the special variables that are always defined
UNITS_SPECIAL_VARS = {'t': second, 'dt': second, 'xi': second**-0.5}
SPECIAL_VARS = UNITS_SPECIAL_VARS.keys()

# Definitions of equation structure for parsing with pyparsing
###############################################################################
# Basic Elements
###############################################################################

# identifiers like in C: can start with letter or underscore, then a
# combination of letters, numbers and underscores
# Note that the check_identifiers function later performs more checks, e.g.
# names starting with underscore should only be used internally
IDENTIFIER = Word(string.ascii_letters + '_',
                  string.ascii_letters + string.digits + '_').setResultsName('identifier')

# very broad definition here, expression will be analysed by sympy anyway
# allows for multi-line expressions, where each line can have comments
EXPRESSION = Combine(OneOrMore((CharsNotIn(':#\n') +
                                Suppress(Optional(LineEnd()))).ignore('#' + restOfLine)),
                     joinString=' ').setResultsName('expression')


# a unit
# very broad definition here, again. Whether this corresponds to a valid unit
# string will be checked later
UNIT = Word(string.ascii_letters + string.digits + '*/. ').setResultsName('unit')

# a single Flag (e.g. "const" or "event-driven")
FLAG = Word(string.ascii_letters + '_-')

# Flags are comma-separated and enclosed in parantheses: "(flag1, flag2)"
FLAGS = (Suppress('(') + FLAG + ZeroOrMore(Suppress(',') + FLAG) +
         Suppress(')')).setResultsName('flags')

###############################################################################
# Equations
###############################################################################
# Three types of equations
# Parameter:
# x : volt (flags)
PARAMETER = Group(IDENTIFIER + Suppress(':') + UNIT +
                  Optional(FLAGS)).setResultsName('parameter')

# Static equation:
# x = 2 * y : volt (flags)
STATIC_EQ = Group(IDENTIFIER + Suppress('=') + EXPRESSION + Suppress(':') +
                  UNIT + Optional(FLAGS)).setResultsName('static_equation')

# Differential equation
# dx/dt = -x / tau : volt
DIFFOP = (Suppress('d') + IDENTIFIER + Suppress('/') + Suppress('dt'))
DIFF_EQ = Group(DIFFOP + Suppress('=') + EXPRESSION + Suppress(':') + UNIT +
                Optional(FLAGS)).setResultsName('diff_equation')

# ignore comments
EQUATION = (PARAMETER | STATIC_EQ | DIFF_EQ).ignore('#' + restOfLine)
EQUATIONS = ZeroOrMore(EQUATION)


def check_identifier_basic(identifier):
    '''
    Check an identifier (usually resulting from an equation string provided by
    the user) for conformity with the rules:
    
        1. Only ASCII characters
        2. Starts with underscore or character, then mix of alphanumerical
           characters and underscore
        3. Is not a reserved keyword of Python
    
    Arguments:
    
    ``identifier``
        The string that should be checked
    
    The function raises a ``ValueError`` if the identifier does not conform to
    the above rules.
    '''
    
    # Check whether the identifier is parsed correctly -- this is always the
    # case, if the identifier results from the parsing of an equation but there
    # might be situations where the identifier is specified directly
    parse_result = list(IDENTIFIER.scanString(identifier))
    
    # parse_result[0][0][0] refers to the matched string -- this should be the
    # full identifier, if not it is an illegal identifier like "3foo" which only
    # matched on "foo" 
    if len(parse_result) != 1 or parse_result[0][0][0] != identifier:
        raise ValueError('"%s" is not a valid variable name.' % identifier)

    if keyword.iskeyword(identifier):
        raise ValueError(('"%s" is a Python keyword and cannot be used as a '
                          'variable.') % identifier)
    
    if identifier.startswith('_'):
        raise ValueError(('Variable "%s" starts with an underscore, '
                          'this is only allowed for variables used '
                          'internally') % identifier)

def check_identifier_reserved(identifier):
    '''
    Check that identifiers do not use the
    '''
    if identifier in SPECIAL_VARS:
        raise ValueError(('"%s" has a special meaning in equations and cannot '
                         ' be used as a variable name.') % identifier)


def check_identifier(identifier):
    '''
    Performs all the registered checks (via
    :meth:`Equations.register_identifier_check`) against ``identifier``, each
    raising a ValueError for illegal identfiers.
    '''
    for check_func in Equations.identifier_checks:
        check_func(identifier)


def parse_string_equations(eqns, namespace, exhaustive, level):
    """
    Parses a string defining equations and returns a dictionary, mapping
    variable names to :class:`Equations._Equation` objects.
    
    Arguments:
    ``namespace``
        An explictly given namespace (dictionary mapping names to objects)
    ``exhaustive``
        Whether the namespace in the namespace argument specifies the
        namespace completely (``True``) or should be used in addition to
        the locals/globals dictionaries (``False``)
    ``level``
        The level in the stack (an integer >=0) where to look for locals
        and globals
    
    """
    equations = {}
    
    try:
        parsed = EQUATIONS.parseString(eqns, parseAll=True)
    except ParseException as p_exc:
        raise ValueError('Parsing failed: \n' + str(p_exc.line) + '\n' +
                         ' '*(p_exc.column - 1) + '^\n' + str(p_exc))
    for eq in parsed:
        eq_type = eq.getName()
        eq_content = dict(eq.items())
        # Check for reserved keywords
        identifier = eq_content['identifier']
        
        # Convert unit string to Unit object
        unit = get_unit_from_string(eq_content['unit'])
        
        expression = eq_content.get('expression', None)
        if not expression is None:
            # Replace multiple whitespaces (arising from joining multiline
            # strings) with single space
            p = re.compile(r'\s{2,}')
            expression = p.sub(' ', expression)
        flags = list(eq_content.get('flags', []))

        equation = Equation(eq_type, identifier, expression, unit, flags,
                            namespace, exhaustive, level + 1) 
        
        if identifier in equations:
            raise ValueError('Duplicate definition of variable "%s"' %
                             identifier)
                                       
        equations[identifier] = equation
    
    return equations            

def resolve_equations(equations, variables):
    '''
    Resolve all the equations in the ``equations`` dictionary (see
    :meth:`CodeString.resolve`), treating the list of ``variables`` as internal
    variables.
    '''
    for eq in equations.itervalues():
        eq.resolve(variables)
    
    namespace = {}
    # Make absolutely sure there are no conflicts and nothing weird is
    # going on
    for eq in equations.itervalues():
        if eq.expr is None:
            # Parameters do not have/need a namespace
            continue
        for key, value in eq.expr._namespace.iteritems():
            if key in namespace:
                # Should refer to exactly the same object
                assert value is namespace[key] 
            else:
                namespace[key] = value
    
    return namespace

class ResolutionConflictWarning(UserWarning):
    '''
    A warning for situations where an identifier can refer to more than one
    object in the namespaces used for resolving identifiers.
    '''
    pass


class CodeString(object):
    '''
    A class for representing strings and an attached namespace.
    '''

    def __init__(self, code, namespace=None, exhaustive=False, level=0):
        '''
        Creates a new :class:`CodeString`.

        If ``exhaustive`` is not ``False`` (meaning that the namespace for the
        string is explicitly specified), the :class:`CodeString` object saves
        the current local and global namespace for later use in resolving
        identifiers.

        Arguments:

        ``code``:
            The code string, may be an expression or a statement (possibly
            multi-line).

        ``namespace``:
            A mapping (e.g. a dictionary), mapping identifiers (strings) to
            objects. Will be used as a namespace for the ``code``.

        ``exhaustive``:
            If set to ``True``, no local/global namespace will be saved,
            meaning that the given namespace has to be exhaustive (except for
            units). Defaults to ``False``, meaning that the given namespace
            augments the local and global namespace (taking precedence over
            them in case of conflicting definitions).

        '''
        self._code = code
        
        # extract identifiers from the code
        self._identifiers = set(get_identifiers(code))
        
        if namespace is None:
            namespace = {}
        
        self._exhaustive = exhaustive
        
        if not exhaustive:
            frame = inspect.stack()[level + 1][0]
            self._locals = frame.f_locals.copy()
            self._globals = frame.f_globals.copy()
        else:
            self._locals = {}
            self._globals = {}
        
        self._given_namespace = namespace
        
        # The namespace containing resolved references
        self._namespace = None
    
    code = property(lambda self: self._code,
                    doc='The code string')

    exhaustive = property(lambda self: self._exhaustive,
                          doc='Whether the namespace is exhaustively defined')
        
    identifiers = property(lambda self: self._identifiers,
                           doc='Set of identifiers in the code string')
    
    is_resolved = property(lambda self: not self._namespace is None,
                           doc='Whether the external identifiers have been resolved')
        
    namespace = property(lambda self: self._namespace,
                         doc='The namespace resolving external identifiers')    
        
    def resolve(self, internal_variables):
        '''
        Determines the namespace for the given codestring, containing
        resolved references to externally defined variables and functions.

        The resulting namespace includes units but does not include anything
        present in the ``internal variables`` collection. All referenced
        internal variables are included in the CodeString's ``dependency``
        attribute. 
        
        Raises an error if a variable/function cannot be resolved and is
        not contained in ``internal_variables``. Raises a
        :class:``ResolutionConflictWarning`` if there are conflicting
        resolutions.
        '''

        if self.is_resolved:
            raise TypeError('Variables have already been resolved before.')

        unit_namespace = get_default_unit_namespace()
        special_variables = ('t', 'dt', 'xi')
        
        namespace = {}
        for identifier in self.identifiers:
            # We save tuples of (namespace description, referred object) to
            # give meaningful warnings in case of duplicate definitions
            matches = []
            if identifier in self._given_namespace:
                matches.append(('user-defined',
                                self._given_namespace[identifier]))
            if identifier in self._locals:
                matches.append(('locals',
                                self._locals[identifier]))
            if identifier in self._globals:
                matches.append(('globals',
                                self._globals[identifier]))
            if identifier in unit_namespace:
                matches.append(('units',
                               unit_namespace[identifier]))
            
            if identifier in SPECIAL_VARS:
                # The identifier is t, dt, or xi
                if len(matches) == 1:
                    warn(('The name "%s" in the code string "%s" has a special '
                          'meaning but also refers to a variable in the %s '
                          'namespace: %r') %
                         (identifier, self.code, matches[0][0], matches[0][1]),
                         ResolutionConflictWarning)
                elif len(matches) > 1:
                    warn(('The name "%s" in the code string "%s" has a special '
                          'meaning but also to refers to variables in the '
                          'following namespaces: %s') %
                         (identifier, self.code, [m[0] for m in matches]),
                         ResolutionConflictWarning)                    
            elif identifier in internal_variables:
                # The identifier is an internal variable
                if len(matches) == 1:
                    warn(('The name "%s" in the code string "%s" refers to an '
                          'internal variable but also to a variable in the %s '
                          'namespace: %r') %
                         (identifier, self.code, matches[0][0], matches[0][1]),
                         ResolutionConflictWarning)
                elif len(matches) > 1:
                    warn(('The name "%s" in the code string "%s" refers to an '
                          'internal variable but also to variables in the '
                          'following namespaces: %s') %
                         (identifier, self.code, [m[0] for m in matches]),
                         ResolutionConflictWarning)
            else:
                # The identifier is not an internal variable
                if len(matches) == 0:
                    raise ValueError('The identifier "%s" in the code string '
                                     '"%s" could not be resolved.' % 
                                     (identifier, self.code))
                elif len(matches) > 1:
                    # Possibly, all matches refer to the same object
                    first_obj = matches[0][1]
                    if not all([m[1] is first_obj for m in matches]):
                        warn(('The name "%s" in the code string "%s" '
                              'refers to different objects in different '
                              'namespaces used for resolving. Will use '
                              'the object from the %s namespace: %r') %
                             (identifier, self.code, matches[0][0],
                              first_obj))
                
                # use the first match (according to resolution order)
                namespace[identifier] = matches[0][1]
                
        self._namespace = namespace

    def frozen(self):
        '''
        Returns a new :class:`CodeString` object, where all external variables
        are replaced by their floating point values and removed from the
        namespace.
        
        The namespace has to be resolved using the :meth:`resolve` method first.
        '''
        
        if not self.is_resolved:
            raise TypeError('Can only freeze resolved CodeString objects.')
        
        #TODO: For expressions, this could be done more elegantly with sympy
        
        new_namespace = self.namespace.copy()
        substitutions = {}
        for identifier in self.identifiers:
            if identifier in new_namespace:
                # Try to replace the variable with its float value
                try:
                    float_value = float(new_namespace[identifier])
                    substitutions[identifier] = str(float_value)
                    # Reference in namespace no longer needed
                    del new_namespace[identifier]
                except (ValueError, TypeError):
                    pass
        
        # Apply the substitutions to the string
        new_code = word_substitute(self.code, substitutions)
        
        # Create a new CodeString object with the new code and namespace
        new_obj = type(self)(new_code, namespace=new_namespace,
                             exhaustive=True)
        new_obj._namespace = new_namespace.copy()
        return new_obj

    def check_linearity(self, variable):
        '''
        Returns whether the expression  is linear with respect to ``variable``,
        assuming that all other variables are constants. The expression should
        not contain any functions.
        '''
        try:
            sympy_expr = sympify(self.code)
        except SympifyError:
            raise ValueError('Expression "%s" cannot be parsed with sympy' %
                             self.code)
    
        x = Symbol(variable)
    
        if not x in sympy_expr:
            return True
    
    #    # This tries to check whether the expression can be rewritten in an a*x + b
    #    # but apparently this does not work very well
    #    a = Wild('a', exclude=[x])
    #    b = Wild('b', exclude=[x])
    #    matches = sympy_expr.match(a * x + b) 
    #
    #    return not matches is None
    
        # This seems to be more robust: Take the derivative with respect to the
        # variable
        diff_f = diff(sympy_expr, x).simplify()
    
        # if the expression is linear, x should have disappeared
        return not x in diff_f 
    
    
    def eval_expr(self, internal_variables):
        '''
        Evaluates the expression ``expr`` in its namespace, augmented by the
        values for the ``internal_variables`` (as a dictionary).
        '''
    
        if not self.is_resolved:
            raise TypeError('Can only evaluate resolved CodeString objects.')
        
        namespace = self.namespace.copy()
        namespace.update(internal_variables)
        return eval(self.code, namespace)
    
    
    def get_expr_dimensions(self, variable_units):
        '''
        Returns the dimensions of the expression by evaluating it in its
        namespace, replacing all internal variables with their units. The units
        have to be given in the mapping ``variable_units``. 
        
        The namespace has to be resolved using the :meth:`resolve` method first.
        
        May raise an DimensionMismatchError during the evaluation.
        '''
        return get_dimensions(self.eval_expr(variable_units))
    
    
    def check_unit_against(self, unit, variable_units):
        '''
        Checks whether the dimensions of the expression match the expected
        dimension of ``unit``. The units of all internal variables have to be
        given in the mapping ``variable_units``. 
        
        The namespace has to be resolved using the :meth:`resolve` method first.
        
        May raise an DimensionMismatchError during the evaluation.
        '''
        expr_dimensions = self.get_expr_dimensions(variable_units)
        expected_dimensions = get_dimensions(unit)
        if not expr_dimensions == expected_dimensions:
            raise DimensionMismatchError('Dimensions of expression does not '
                                         'match its definition',
                                         expr_dimensions, expected_dimensions)
    
    def split_stochastic(self):
        '''
        Splits the expression into a tuple of two :class:`CodeString` objects
        f and g, assuming an expression of the form ``f + g * xi``, where
        ``xi`` is the symbol for the random variable.
        
        If no ``xi`` symbol is present in the code string, a tuple
        ``(self, None)`` will be returned with the unchanged
        :class:`CodeString` object.
        '''
        s_expr = sympify(self.code)
        xi = Symbol('xi')
        if not xi in s_expr:
            return (self, None)
        
        f = Wild('f', exclude=[xi]) # non-stochastic part
        g = Wild('g', exclude=[xi]) # stochastic part
        matches = s_expr.match(f + g * xi)
        if matches is None:
            raise ValueError(('Expression "%s" cannot be separated into stochastic '
                             'and non-stochastic term') % self.code)
    
        f_expr = CodeString(str(matches[f]), namespace=self.namespace.copy(),
                            exhaustive=True)
        g_expr = CodeString(str(matches[g] * xi), namespace=self.namespace.copy(),
                            exhaustive=True)
        
        return (f_expr, g_expr)

    def __str__(self):
        return self.code
    
    def __repr__(self):
        return '%s(%r)' % (self.__class__.__name__, self.code)


class Equation(object):
    '''
    Class for internal use, encapsulates a single equation or parameter.
    '''
    def __init__(self, eq_type, varname, expr, unit, flags,
                 namespace, exhaustive, level):
        '''
        Create a new :class:`_Equation` object.
        '''
        self.eq_type = eq_type
        self.varname = varname
        if eq_type != 'parameter':
            self.expr = CodeString(expr, namespace=namespace,
                                   exhaustive=exhaustive, level=level + 1)
        else:
            self.expr = None
        self.unit = unit
        self.flags = flags
        
        # will be set later in the sort_static_equations method of Equations
        self.update_order = -1

    # parameters do not depend on time
    is_time_dependent = property(lambda self: self.expr.is_time_dependent
                                 if not self.expr is None else False,
                                 doc='Whether this equation is time dependent')

    def resolve(self, internal_variables):
        '''
        Resolve all the variables (see :meth:`CodeString.resolve`),
        treating the list ``internal_variables`` as internal variables.
        '''
        if not self.expr is None:
            self.expr.resolve(internal_variables)        

    def __str__(self):
        if self.eq_type == 'diff_equation':
            s = 'd' + self.varname + '/dt'
        else:
            s = self.varname
        
        if not self.expr is None:
            s += ' = ' + str(self.expr)
        
        s += ' : ' + str(self.unit)
        
        if len(self.flags):
            s += '(' + ', '.join(self.flags) + ')'
        
        return s
    
    def __repr__(self):
        s = '<' + EQUATION_TYPE[self.eq_type] + ' ' + self.varname
        
        if not self.expr is None:
            s += ': ' + self.expr.code

        s += ' (Unit: ' + str(self.unit)
        
        if len(self.flags):
            s += ', flags: ' + ', '.join(self.flags)
        
        s += ')>'
        return s

class Equations(object):
    """Container that stores equations from which models can be created.
    
    Initialised as::
    
        Equations(eqs[, namespace=None][, exhaustive=False][, level=0])
    
    with arguments:
    
    ``eqs``
        A multiline string of equations (see below)
    ``namespace=None``
        An explictly given namespace (dictionary mapping names to objects)
    ``exhaustive=False``
        Whether the namespace in the namespace argument specifies the
        namespace completely (``True``) or should be used in addition to
        the locals/globals dictionaries (``False``)
    ``level=0``
        The level in the stack (an integer >=0) where to look for locals
        and globals 
           
    **String equations**
    
    String equations can be of any of the following forms:
    
    (1) ``dx/dt = f : unit (flags)`` (differential equation)
    (2) ``x = f : unit (flags)`` (equation)
    (3) ``x : unit (flags)`` (parameter)
    
    Equations can span several line and contain Python-style comments starting
    with ``#``
    
    """

    def __init__(self, eqns, namespace=None, exhaustive=False, level=0):
        '''
        Constructs a new equations object from the multiline string ``eqns``,
        see :class:`Equations` for more details.
        '''
                
        self._equations = parse_string_equations(eqns, namespace, exhaustive,
                                                  level + 1)

        # Do a basic check for the identifiers
        self.check_identifiers()
        
        # Check for special symbol xi (stochastic term)
        uses_xi = None
        for eq in self._equations.itervalues():
            if not eq.expr is None and 'xi' in eq.expr.identifiers:
                if not eq.eq_type == 'diff_equation':
                    raise ValueError(('The equation defining %s contains the '
                                      'symbol "xi" but is not a differential '
                                      'equation.') % eq.varname)
                elif not uses_xi is None:
                    raise ValueError(('The equation defining %s contains the '
                                      'symbol "xi", but it is already used '
                                      'in the equation defining %s.') %
                                     (eq.varname, uses_xi))
                else:
                    uses_xi = eq.varname
        
        # Build the namespaces, resolve all external variables and rearrange
        # static equations
        self._namespace = resolve_equations(self._equations, self.variables)
        
        # Check the units for consistency
        self.check_units()

    def __iter__(self):
        return iter(self.equations.iteritems())

    # Class attribute: A set of functions that are used to check identifiers
    # Functions can be registered with the static method 
    # `:meth:Equations.register_identifier_check` and will be automatically
    # used when checking identifiers
    identifier_checks = set([check_identifier_basic,
                             check_identifier_reserved])
    
    @staticmethod
    def register_identifier_check(func):
        if not hasattr(func, '__call__'):
            raise ValueError('Can only register callables.')
        
        Equations.identifier_checks.add(func)

    def _get_substituted_expressions(self):
        '''
        Returns a list of ``(varname, expr)`` tuples, containing all
        differential equations (``expr`` is a :class:`CodeString` object)
        with all the static equation variables substituted with the respective
        expressions.
        '''
        sub_exprs = []
        substitutions = {}        
        for eq in self.equations_ordered:
            # Skip parameters
            if eq.expr is None:
                continue
            
            expr = CodeString(word_substitute(eq.expr.code, substitutions),
                              self._namespace, exhaustive=True)
            
            if eq.eq_type == 'static_equation':
                substitutions.update({eq.varname: '(%s)' % expr.code})
            elif eq.eq_type == 'diff_equation':
                #  a differential equation that we have to check                                
                expr.resolve(self.names)
                sub_exprs.append((eq.varname, expr))
            else:
                raise AssertionError('Unknown equation type %s' % eq.eq_type)
        
        return sub_exprs        

    def _is_linear(self, conditionally_linear=False):
        '''
        Whether all equations are linear and only refer to constant parameters.
        if ``conditionally_linear`` is ``True``, only checks for conditional
        linearity (i.e. all differential equations are linear with respect to
        themselves but not necessarily with respect to other differential
        equations).
        '''
        expressions = self.substituted_expressions
                
        for varname, expr in expressions:                
            identifiers = expr.identifiers
            
            # Check that it does not depend on time
            if 't' in identifiers:
                return False
            
            # Check that it does not depend on non-constant parameters
            for parameter in self.parameter_names:
                if (parameter in identifiers and
                    not 'constant' in self.equations[parameter].flags):
                    return False

            if conditionally_linear:
                # Check for linearity against itself
                if not expr.check_linearity(varname):
                    return False
            else:
                # Check against all state variables (not against static
                # equation variables, these are already replaced)
                for diff_eq_var in self.diff_eq_names:                    
                    if not expr.check_linearity(diff_eq_var):
                        return False

        # No non-linearity found
        return True

    def _get_units(self):
        '''
        Dictionary of all internal variables (including t, dt, xi) and their
        corresponding units
        '''
        units = dict([(var, eq.unit) for var, eq in
                      self._equations.iteritems()])
        units.update(UNITS_SPECIAL_VARS)
        return units

    # Properties
    
    equations = property(lambda self: self._equations,
                        doc='A dictionary mapping variable names to equations')
    equations_ordered = property(lambda self: sorted(self._equations.itervalues(),
                                                     key=lambda key: key.update_order),
                                 doc='A list of all equations, sorted '
                                 'according to the order in which they should '
                                 'be updated')
    
    diff_eq_expressions = property(lambda self: [(varname, eq.expr.frozen()) for 
                                                 varname, eq in self.equations.iteritems()
                                                 if eq.eq_type == 'diff_equation'],
                                  doc='A list of (variable name, expression) '
                                  'tuples of all differential equations.')
    
    eq_expressions = property(lambda self: [(varname, eq.expr.frozen()) for 
                                            varname, eq in self.equations.iteritems()
                                            if eq.eq_type in ('static_equation',
                                                              'diff_equation')],
                                  doc='A list of (variable name, expression) '
                                  'tuples of all equations.') 
    
    substituted_expressions = property(_get_substituted_expressions)
    
    names = property(lambda self: [eq.varname for eq in self.equations_ordered])
    
    diff_eq_names = property(lambda self: [eq.varname for eq in self.equations_ordered
                                           if eq.eq_type == 'diff_equation'])
    static_eq_names = property(lambda self: [eq.varname for eq in self.equations_ordered
                                           if eq.eq_type == 'static_equation'])
    eq_names = property(lambda self: [eq.varname for eq in self.equations_ordered
                                           if eq.eq_type in ('diff_equation', 'static_equation')])
    parameter_names = property(lambda self: [eq.varname for eq in self.equations_ordered
                                             if eq.eq_type == 'parameter'])    
    
    is_linear = property(_is_linear)
    
    is_conditionally_linear = property(lambda self: self._is_linear(conditionally_linear=True),
                                       doc='Whether all equations are conditionally linear')
    
    units = property(_get_units)
    
    variables = property(lambda self: set(self.units.keys()),
                         doc='Set of all variables')
    
    def _sort_static_equations(self):
        '''
        Sorts the static equations in a way that resolves their dependencies
        upon each other. After this method has been run, the static equations
        returned by the ``equations_ordered`` property are in the order in which
        they should be updated
        '''
        
        # Get a dictionary of all the dependencies on other static equations,
        # i.e. ignore dependencies on parameters and differential equations
        static_deps = {}
        for eq in self._equations.itervalues():
            if eq.eq_type == 'static_equation':
                static_deps[eq.varname] = [dep for dep in eq.identifiers if
                                           dep in self._equations and
                                           self._equations[dep].eq_type == 'static_equation']
        
        # Use the standard algorithm for topological sorting:
        # http://en.wikipedia.org/wiki/Topological_sorting
                
        # List that will contain the sorted elements
        sorted_eqs = [] 
        # set of all nodes with no incoming edges:
        no_incoming = set([var for var, deps in static_deps.iteritems()
                           if len(deps) == 0]) 
        
        while len(no_incoming):
            n = no_incoming.pop()
            sorted_eqs.append(n)
            # find variables m depending on n
            dependent = [m for m, deps in static_deps.iteritems()
                         if n in deps]
            for m in dependent:
                static_deps[m].remove(n)
                if len(static_deps[m]) == 0:
                    # no other dependencies
                    no_incoming.add(m)
        if any([len(deps) > 0 for deps in static_deps.itervalues()]):
            raise ValueError('Cannot resolve dependencies between static '
                             'equations, dependencies contain a cycle.')
        
        # put the equations objects in the correct order
        for order, static_variable in enumerate(sorted_eqs):
            self._equations[static_variable].update_order = order
        
        # Sort differential equations and parameters after static equations
        for eq in self._equations.itervalues():
            if eq.eq_type == 'diff_equation':
                eq.update_order = len(sorted_eqs)
            elif eq.eq_type == 'parameter':
                eq.update_order = len(sorted_eqs) + 1

    def check_units(self):
        '''
        Check all the units for consistency and raise a 
        :class:`DimensionMismatchError` in case of errors.
        '''
        units = self.units
        for var, eq in self._equations.iteritems():
            if eq.eq_type == 'parameter':
                # no need to check units for parameters
                continue
            
            if eq.eq_type == 'diff_equation':
                try:
                    eq.expr.check_unit_against(units[var] / second, units)
                except DimensionMismatchError as dme:
                    raise DimensionMismatchError(('Differential equation defining '
                                                  '%s does not use consistent units: %s') % 
                                                 (var, dme.desc), *dme.dims)
            elif eq.eq_type == 'static_equation':
                try:
                    eq.expr.check_unit_against(units[var], units)
                except DimensionMismatchError as dme:
                    raise DimensionMismatchError(('Static equation defining '
                                                  '%s does not use consistent units: %s') % 
                                                 (var, dme.desc), *dme.dims)                
            else:
                raise AssertionError('Unknown equation type: "%s"' % eq.eq_type)

    def check_identifiers(self):
        '''
        Checks the list of identifiers used in this equation against the given
        list of reserved identifiers (also performs some standard checks like
        not allowing Python keywords, see :func:`check_identifier_basic`).
        '''
        for name in self.names:            
            check_identifier(name)

    def check_flags(self, allowed_flags):
        '''
        Checks the list of flags against the flags contained in
        ``allowed_flags``, which should be a dictionary mapping equation types
        (``parameter``, ``diff_equation``, ``static_equation``) to a list
        of strings (the allowed flags for that equation type). Not specifying
        allowed flags for an equation type is the same as specifying an empty
        list for it.
        '''
        for eq in self.equations.itervalues():
            for flag in eq.flags:
                if not eq.eq_type in allowed_flags or len(allowed_flags[eq.eq_type]) == 0:
                    raise ValueError('Equations of type "%s" cannot have any flags.' % EQUATION_TYPE[eq.eq_type])
                if not flag in allowed_flags[eq.eq_type]:
                    raise ValueError(('Equations of type "%s" cannot have a '
                                      'flag "%s", only the following flags '
                                      'are allowed: %s') % (EQUATION_TYPE[eq.eq_type],
                                                            flag, allowed_flags[eq.eq_type]))

    #
    # Representation
    # 

    def __str__(self):
        strings = [str(eq) for eq in self._equations.itervalues()]
        return '\n'.join(strings)

    def _repr_pretty_(self, p, cycle):
        ''' Pretty printing for ipython '''
        if cycle: 
            # Should never happen actually
            return 'Equations(...)'
        for eq in self._equations.itervalues():
            p.pretty(eq)
