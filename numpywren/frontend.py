import ast
import astor
import time
import dill
import logging
import abc
from numpywren.matrix import BigMatrix
from numpywren.matrix_init import shard_matrix
from numpywren import exceptions, compiler, utils
import numpy as np
import asyncio
from numpywren.kernels import *
from collections import namedtuple
import inspect
from operator import *
from numpywren import lambdapack as lp
from scipy.linalg import qr
import sympy
from numbers import Number

op_table = {}
op_table['Add'] = add
op_table['Div'] = truediv
op_table['And'] = and_
op_table['Not'] = and_
op_table['Mult'] = mul
op_table['Mul'] = mul
op_table['Sub'] = sub
op_table['Mod'] = mod
op_table['Pow'] = pow
op_table['Or'] = or_
op_table['EQ'] = eq
op_table['NE'] = ne
op_table['Neg'] = neg
op_table['Not'] = not_
op_table['LT'] = lt
op_table['LE'] = le
op_table['GE'] = ge
op_table['GT'] = gt
op_table['ceiling'] = sympy.ceiling
op_table['floor'] = sympy.floor
op_table['log'] = sympy.log


logger = logging.getLogger('numpywren')

''' front end parser + typechecker for lambdapack

comp_op: '<'|'>'|'=='|'>='|'<='
un_op : '-' | 'not'
_op ::=  + | - | * | ** | / | // | %
var ::= NAME
term ::= NAME | INT | FLOAT | expr
un_expr ::= un_op term | term
mul_expr ::= un_expr (('/'|'*') un_expr)*
arith_expr ::= mul_expr ((‘+’|’-’) mul_expr)*
simple_expr ::= mul_expr (‘**’ mul_expr)*
comparison: expr (comp_op expr)*
mfunc_expr = mfunc(expr)
expr ::= simple_expr | m_func_expr | comparison
m_func ::=  ceiling | floor | log
index_expr ::= NAME ‘[‘ expr (, expr)* ‘]’
op := NAME
_arg := (index_expr | expr)
_index_expr_assign ::= index_expr (, index_expr)* ‘=’ op(_arg (, _arg)*
_var_assign ::= var ‘=’ expr
assign_stmt ::= _index_expr_assign | _var_assign
block := stmt (NEW_LINE stmt)*
for_stmt ::= 'for' var 'in' ‘range(‘expr, expr’)’  ':' block
if_stmt: 'if' expr ':' block  ['else' ':' block]
stmt ::= for_stmt | with_stmt | assign_stmt | expr
'''

KEYWORDS = ["ceiling", "floor", "log", "REDUCTION_LEVEL"]
M_FUNCS = ['ceiling', 'floor', 'log']
M_FUNC_OUT_TYPES = {}
M_FUNC_OUT_TYPES['ceiling'] = int
M_FUNC_OUT_TYPES['floor'] = int
M_FUNC_OUT_TYPES['log'] = float
M_FUNC_OUT_TYPES['log2'] = float

class Expression(abc.ABC):
    pass

class LambdaPackType(abc.ABC):
    pass

class LambdaPackAttributes(abc.ABC):
    pass

class NullType(LambdaPackType):
    pass

class PrimitiveType(LambdaPackType):
    pass

class NumericalType(PrimitiveType):
    pass

class IntType(NumericalType):
    pass

class LinearIntType(IntType):
    pass

class Const(LambdaPackAttributes):
    pass

class BoolType(PrimitiveType):
    pass

class ConstBoolType(BoolType, Const):
    pass

class FloatType(NumericalType):
    pass

class ConstIntType(LinearIntType, Const):
    pass

class ConstFloatType(FloatType, Const):
    pass

class IndexExprType(LambdaPackType):
    pass

class BigMatrixType(LambdaPackType):
    pass

RangeVar = namedtuple("RangeVar", ["var", "start", "end", "step"])
RemoteCallAbstractWithContext = namedtuple("RemoteCallAbstractWithContext", ["remote_call", "scope"])

## Exprs ##
class BinOp(ast.AST, Expression):
    _fields = ['op', 'left', 'right', 'type']

class CmpOp(ast.AST, Expression):
    _fields = ['op', 'left', 'right', 'type']

class UnOp(ast.AST, Expression):
    _fields = ['op', 'e', 'type']

class Mfunc(ast.AST, Expression):
    _fields = ['op', 'e', 'type']

class Assign(ast.AST):
    _fields = ['lhs', 'rhs']

class Ref(ast.AST, Expression):
    _fields = ['name', 'type']

class IntConst(ast.AST, Expression):
    _fields = ['val', 'type']

class FloatConst(ast.AST, Expression):
    _fields = ['val', 'type']

class BoolConst(ast.AST, Expression):
    _fields = ['val','type']

class Block(ast.AST):
    _fields = ['body',]

class If(ast.AST):
    _fields = ['cond', 'body', 'elseBody']

    def __init__(self, cond, body, elseBody=[]):
        return super().__init__(cond, body, elseBody)

class Attr(ast.AST):
    _fields = ['obj', 'attr_name']

class Stargs(ast.AST):
    _fields = ['args']

class For(ast.AST):
    _fields = ['var', 'min', 'max', 'step', 'body']

class Return(ast.AST):
    _fields = ['val',]

class FuncDef(ast.AST):
    _fields = ['name', 'args', 'body', 'arg_types']

class RemoteCallAbstract(ast.AST):
    _fields = ['compute', 'output', 'args', 'kwargs', 'type']

class Reduction(ast.AST):
    _fields = ['var', 'min', 'max', 'expr', 'b_fac', 'remote_call', 'recursion']

class IndexExpr(ast.AST):
    _fields = ['matrix_name', 'indices']

class Slice(ast.AST):
    _fields = ['low', 'high', 'step', 'type']

class Return(ast.AST):
    _fields = ['value', 'type']

class ReducerCall(ast.AST):
    _fields = ['name','function', 'args', 'type']

REDUCTION_SPECIALS = ["level", "reduce_args", "reduce_next", "reduce_idxs"]

def unify(type_list):
    if (len(type_list) == 1): return type_list[0]
    if (len(type_list) < 3):
        t0 = type_list[0]
        t1 = type_list[0]
        if (issubclass(t0, t1)):
            return t1
        elif (issubclass(t1, t0)):
            return t0
        else:
            raise exceptions.LambdaPackTypeException("Non unifiable types {0} vs {1}".format(type_list))
    else:
        t0,t1 = type_list[0], type_list[1]
        t01 = unify([t0, t1])
        return unify([t01] + type_list[2:])



class LambdaPackParse(ast.NodeVisitor):
    """
    Translate a lambdapack expression.
    """
    def __init__(self):
        self.in_if = False
        self.in_else = False
        self.for_loops = 0
        self.max_for_loop_id = -1
        self.current_for_loop = -1
        self.decl_dict = {}
        self.return_node = None
        self.in_reduction = False
        self.current_reduction_object = None
        self.reduce_next_exprs = []
        super().__init__()


    def visit_Num(self, node):
        if isinstance(node.n, int):
            return IntConst(node.n, None)
        elif isinstance(node.n, float):
            return FloatConst(node.n, None)
        else:
            raise NotImplementedError("Only Integers and Floats supported")

    def visit_BoolOp(self, node):
        values = [self.visit(x) for x in node.values]

        left = self.visit(node.values[0])
        right = self.visit(node.values[1])
        op  = node.op
        if (isinstance(op, ast.Or)):
            op = "Or"
        elif (isinstance(op, ast.And)):
            op = "And"
        else:
            raise Exception("Invalid bool operation {0}".format(op))
        i = 1
        lhs = left
        while (i < len(values)):
            lhs = BinOp(op, lhs, values[i], None)
            i += 1
        return lhs

    def visit_BinOp(self, node):
        VALID_BOPS  = ["Add", "Sub", "Mult", "Div", "Mod",  "Pow", "FloorDiv", "And", "Or"]
        left = self.visit(node.left)
        right = self.visit(node.right)
        op  = node.op.__class__.__name__
        if (op not in VALID_BOPS):
            raise NotImplementedError("Unsupported BinOp {0}".format(op))
        ret = BinOp(op, left, right, None)
        return ret

    def visit_Str(self, node):
        raise NotImplementedError("Stings not supported")

    def visit_Compare(self, node):
        VALID_COMP_OPS = ["EQ", "NE", "LT", "GT",  "LE", "GE"]
        left = self.visit(node.left)
        if (len(node.ops) != 1):
            raise NotImplementedError("Only single op compares supported")

        if (len(node.comparators) != 1):
            raise NotImplementedError("Only single comparator compares supported")

        right = self.visit(node.comparators[0])
        op  = node.ops[0].__class__.__name__
        s_op = op.upper()
        if (s_op not in VALID_COMP_OPS):
            raise NotImplementedError("Unsupported CmpOp {0}".format(s_op))
        return CmpOp(s_op, left, right, None)

    def visit_UnaryOp(self, node):
        e = self.visit(node.operand)

        op = node.op.__class__.__name__
        if (op == "USub"):
            s_op = "Neg"
        elif (op == "Not"):
            s_op = "Not"
        else:
            raise NotImplementedError("Unsupported unary operation {0}".format(op))
        return UnOp(s_op, e, None)


    def visit_Name(self, node):
        return Ref(node.id, None)

    def visit_NameConstant(self, node):
        if (node.value == True):
            return IntConst(1, None)
        elif (node.value == False):
            return IntConst(0, None)
        else:
            raise exceptions.LambdaPackParsingException("Unsupported Name constant")


    def visit_Attribute(self, node):
        assert self.in_reduction, "Only Valid Attribute calls are to reducers"
        name = node.value.id
        assert self.decl_dict[name] == self.current_reduction_object, "Incorrect use of reduction features"
        assert node.attr in REDUCTION_SPECIALS, "Only a few special reduction special function calls are valid : {0}".format(REDUCTION_SPECIALS)
        return ReducerCall(name, node.attr, None, None)

    def visit_Call(self, node):
        func = self.visit(node.func)
        kwargs = {x.arg : self.visit(x.value) for x in node.keywords}
        args = [self.visit(x) for x in node.args]
        if (isinstance(func, ReducerCall)):
            return ReducerCall(func.name, func.function, args, None)

        if (isinstance(func, Ref)):
            if (func.name in M_FUNCS):
                assert len(node.args) == 1, "m_func calls must single argument"
                return Mfunc(func.name, self.visit(node.args[0]), None)
            else:
                try:
                    #TODO do this without eval
                    node_func_obj = eval(func.name)
                    if (callable(node_func_obj)):
                        args = [self.visit(x) for x in node.args]
                        return RemoteCallAbstract(node_func_obj, None, args, None, None)
                except NameError:
                    pass
        raise Exception("unsupported function {0}".format(func.name))

    def visit_Assign(self, node):
        rhs = self.visit(node.value)
        if (isinstance(rhs, Expression)):
            if (len(node.targets) != 1):
                raise NotImplementedError("Multiple targets only supported for RemoteOps")
            lhs = self.visit(node.targets[0])
            assign = Assign(lhs, rhs)
            if (self.in_if):
                self.decl_dict[lhs.name]  = rhs
            elif (self.in_else):
                if (lhs.name not in self.decl_dict):
                    raise exceptions.LambdaPackParsingException("Variable {0} declared in else but not in if".format(lhs.name))
                del self.decl_dict[lhs.name]
            else:
                if (lhs.name) in self.decl_dict:
                    raise exceptions.LambdaPackParsingException("multiple variable declarations forbidden")
                self.decl_dict[lhs.name] = rhs
            return assign
        elif isinstance(rhs, RemoteCallAbstract):
            lhs = self.visit(node.targets[0])
            return RemoteCallAbstract(rhs.compute, lhs, rhs.args, rhs.kwargs, rhs.type)
        else:
            raise NotImplementedError("Only assignments of expressions and remote calls supported")


    def visit_If(self, node):
        cond = self.visit(node.test)
        self.in_if = True
        tmp_decl_dict = self.decl_dict.copy()
        body = [self.visit(x) for x in node.body]
        tmp_decl_dict_2 = self.decl_dict.copy()
        self.in_if = False
        self.in_else = True
        else_body = [self.visit(x) for x in node.orelse]
        self.in_else = False
        tmp_decl_dict_3 = self.decl_dict.copy()
        if (list(tmp_decl_dict_3.keys())  != list(tmp_decl_dict.keys())):
            raise exceptions.LambdaPackParsingException("if/else didn't have symmetric pair of declarations")
        for k,v in tmp_decl_dict_2.items():
            if (k in tmp_decl_dict) and (tmp_decl_dict[k] is not tmp_decl_dict_2[k]):
                raise exceptions.LambdaPackParsingException("repeat decl in if clause: {0}".format(k))
            self.decl_dict[k] = v
        return If(cond, body, else_body)

    def visit_FunctionDef(self, func):
        args = [x.arg for x in func.args.args]
        if (len(set(args)) != len(args)):
            raise exceptions.LambdaPackParsingException("No repeat arguments allowed")
        annotations = [eval(x.annotation.id) for x in func.args.args]
        name = func.name
        assert isinstance(func.body, list)
        body = [self.visit(x) for x in func.body]
        #assert func.returns is not None, "LambdaPack functions must have explicit return type"
        return FuncDef(func.name, args, body, annotations)

    def visit_Starred(self, node):
        return Stargs(self.visit(node.value))

    def visit_For(self, node):
        iter_node = node.iter
        prev_for = self.current_for_loop
        self.for_loops += 1
        self.current_for_loop += 1
        is_call = isinstance(iter_node, ast.Call)
        if (is_call):
            is_range = iter_node.func.id == "range"
        else:
            is_range = False

        if (not is_range):
            raise NotImplementedError("Only for(x in range(...)) loops allowed")


        if (len(iter_node.args) == 1):
            start = IntConst(0, None)
            end = self.visit(iter_node.args[0])
        else:
            start = self.visit(iter_node.args[0])
            end = self.visit(iter_node.args[1])
        if (len(iter_node.args) < 3):
            step = IntConst(1, None)
        else:
            step = self.visit(iter_node.args[2])

        body = [self.visit(x) for x in node.body]
        var = node.target.id
        self.decl_dict[var] = iter_node
        self.current_for_loop = prev_for
        self.for_loops -= 1
        self.current_for_loop -= 1
        return For(var, start, end, step, body)

    def visit_Return(self, node):
        raise exceptions.LambdaPackParsingException("returns forbidden in lambdapack, pass in outputs as function arguments")


    def visit_Index(self, node):
        return self.visit(node.value)

    def visit_Tuple(self, node):
        return [self.visit(x) for x in node.elts]

    def visit_ExtSlice(self, node):
        return [self.visit(x) for x in node.dims]

    def visit_Subscript(self, node):
        index = node.slice
        matrix_id = node.value.id
        idxs = self.visit(index)
        return IndexExpr(matrix_id, idxs)

    def visit_Slice(self, node):
        if (node.lower is not None):
            low = self.visit(node.lower)
        else:
            low = None
        if (node.upper is not None):
            high = self.visit(node.upper)
        else:
            high = None
        if (node.step is not None):
            step = self.visit(node.step)
        else:
            step = None
        return Slice(low, high, step, None)

    def visit_Expr(self, node):
        return self.visit(node.value)


def python_type_to_lp_type(p_type, const=False):
    if (p_type is None):
        return NullType
    if (issubclass(p_type, int)):
        if (const):
            return ConstIntType
        else:
            return IntType
    elif (issubclass(p_type, float)):
        if (const):
            return ConstFloatType
        else:
            return FloatType
    elif (issubclass(p_type, bool)):
        return BoolType
    elif (issubclass(p_type, BigMatrix)):
        return BigMatrixType
    else:
        raise exceptions.LambdaPackTypeException("Unsupported Python type: {0}".format(p_type))



class LambdaPackTypeCheck(ast.NodeVisitor):
    ''' Assign a type to every node or throw TypeError
        * For loop bounds needs to be an integer
        * Reduction bounds need to be integers
        * Input to IndexExprs must be a LinearIntType
        * LinearIntType (*|/) ConstIntType -> LinearIntType
        * LinearIntType (//|%|**) ConstIntType -> IntType
        * LinearIntType (*|/|//|%) LinearIntType -> IntType
        * LinearIntType (+/-) LinearIntType -> LinearIntType
        * MFunc(ConstIntType) -> (ConstFloatType, ConstIntType)
        * MFunc(LinearIntType) -> (IntType, FloatType)
    '''
    def __init__(self):
        self.decl_types = {}
        pass

    def visit_FuncDef(self, func):
        annotations = [python_type_to_lp_type(x, const=True) for x in func.arg_types]

        args = [x for x in func.args]
        for arg,anot in zip(args, annotations):
            self.decl_types[arg] = anot
        body = [self.visit(x) for x in func.body]
        return FuncDef(func.name, args, body, annotations)

    def visit_Ref(self, node):
        decl_type = self.decl_types[node.name]
        if (decl_type is None):
            raise LambdaPackTypeException("Refs must be typed")
        return Ref(node.name, decl_type)


    def visit_Assign(self, node):
        rhs = self.visit(node.rhs)
        lhs = node.lhs
        if (lhs.name in self.decl_types):
            is_subclass = issubclass(rhs.type, self.decl_types[lhs.name])
            is_superclass  = issubclass(self.decl_types[lhs.name], rhs.type)
            if ((not is_subclass) and (not is_superclass)):
                raise exceptions.LambdaPackTypeException("Variables must be of unifiable type, {0} is type {1} but was assigned {2}".format(lhs.name, self.decl_types[lhs.name], rhs.type))
        else:
            self.decl_types[lhs.name] = rhs.type
        lhs = self.visit(node.lhs)
        return Assign(lhs, rhs)

    def visit_If(self, node):
        cond = self.visit(node.cond)
        if (not issubclass(cond.type, BoolType)):
            raise exceptions.LambdaPackTypeException("cond of if statement must be BoolType")
        body = [self.visit(x) for x in node.body]
        else_body = [self.visit(x) for x in node.elseBody]
        return If(cond, body, else_body)

    def visit_BinOp(self, node):
        right = self.visit(node.right)
        left = self.visit(node.left)
        r_type = right.type
        l_type = left.type
        op = node.op
        if (op == "Or" or op == "And"):
            assert(issubclass(left.type, BoolType))
            assert(issubclass(right.type, BoolType))
            out_type = BoolType
        else:
            if ((r_type is None) or (l_type is None)):
                raise LambdaPackTypeException("BinOp arguments must be typed")
            type_set = set([r_type, l_type])
            for t in type_set:
                if (not issubclass(t, NumericalType)):
                    raise LambdaPackTypeException("BinOp arguments must be Numerical")
            if (op == "Add" or op == "Sub"):
                # arith type algebra
                if (issubclass(r_type, ConstIntType) and issubclass(l_type, ConstIntType)):
                    out_type = ConstIntType
                elif (issubclass(r_type, LinearIntType) and issubclass(l_type, LinearIntType)):
                    out_type = LinearIntType
                elif (issubclass(r_type, IntType) and issubclass(l_type, IntType)):
                    out_type = IntType
                elif (issubclass(r_type, ConstFloatType) and issubclass(l_type, ConstFloatType)):
                    out_type = ConstFloatType
                elif (issubclass(r_type, ConstFloatType) and issubclass(l_type, ConstIntType)):
                    out_type = ConstFloatType
                elif (issubclass(l_type, ConstFloatType) and issubclass(r_type, ConstIntType)):
                    out_type = ConstFloatType
                elif (issubclass(r_type, FloatType) or issubclass(l_type, FloatType)):
                    out_type = FloatType
                else:
                    raise exceptions.LambdaPackTypeException("Unsupported type combination for add/sub")
            elif (op =="Mult"):
                # mul type algebra
                if (issubclass(r_type, LinearIntType) and issubclass(l_type, ConstIntType)):
                    out_type = LinearIntType
                if (issubclass(r_type, LinearIntType) and issubclass(l_type, LinearIntType)):
                    out_type = IntType
                elif (issubclass(r_type, IntType) and issubclass(l_type, IntType)):
                    out_type = IntType
                elif (issubclass(r_type, ConstFloatType) and issubclass(l_type, ConstFloatType)):
                    out_type = ConstFloatType
                elif (issubclass(r_type, ConstFloatType) and issubclass(l_type, ConstIntType)):
                    out_type = ConstFloatType
                elif (issubclass(r_type, FloatType) or issubclass(l_type, FloatType)):
                    out_type = FloatType
                else:
                    raise exceptions.LambdaPackTypeException("Unsupported type combination for mul")
            elif (op =="Div"):
                # div type algebra
                if (issubclass(r_type, LinearIntType) and issubclass(l_type, ConstIntType)):
                    out_type = LinearIntType
                elif (issubclass(r_type, Const) and issubclass(l_type, Const)):
                    out_type = ConstFloatType
                else:
                    out_type = FloatType
            elif (op == "Mod"):
                if (issubclass(r_type, ConstIntType) and issubclass(l_type, ConstIntType)):
                    out_type = ConstIntType
                elif (issubclass(r_type, IntType) and issubclass(l_type, IntType)):
                    out_type = IntType
                else:
                    out_type = FloatType
            elif (op == "Pow"):
                if (issubclass(r_type, ConstIntType) and issubclass(l_type, ConstIntType)):
                    out_type = ConstIntType
                elif (issubclass(r_type, IntType) and issubclass(l_type, IntType)):
                    out_type = IntType
                elif (issubclass(r_type, Const) and issubclass(l_type, Const)):
                    out_type = ConstFloatType
                else:
                    out_type = FloatType
            elif (op == "FloorDiv"):
                if (issubclass(r_type, ConstIntType) and issubclass(l_type, ConstIntType)):
                    out_type = ConstIntType
                else:
                    out_type = IntType
        return BinOp(node.op, left, right, out_type)

    def visit_Return(self, node):
        r_vals = []
        if (isinstance(node.value, list)):
            for v in node.value:
                r_vals.append(self.visit(v))
        else:
            r_vals.append(self.visit(node.value))
        self.return_node_type = unify([x.type for x in r_vals])
        return Return(r_vals, self.return_node_type)

    def visit_IndexExpr(self, node):
        if (isinstance(node.indices, list)):
            idxs = [self.visit(x) for x in node.indices]
        else:
            idxs = [self.visit(node.indices)]

        out_type = unify([x.type for x in idxs])
        if (not issubclass(out_type, IntType)):
            print("out_type", out_type)
            raise exceptions.LambdaPackTypeException("Indices in IndexExprs must all of type LinearIntType {0}[{1}]".format(node.matrix_name, [str(x) for x in node.indices]))
        return IndexExpr(node.matrix_name, idxs)

    def visit_Stargs(self, node):
       args = self.visit(node.args)
       return Stargs(args)

    def visit_Slice(self, node):
        if (node.low is not None):
            low = self.visit(node.low)
            low_type = low.type
        else:
            low = None
            low_type = LinearIntType
        if (node.high is not None):
            high = self.visit(node.high)
            high_type = high.type
        else:
            high = None
            high_type = LinearIntType
        if (node.step is not None):
            step = self.visit(node.step)
            step_type = step.type
        else:
            step = None
            step_type = LinearIntType
        out_type = unify([low_type, high_type, step_type])
        return Slice(low, high, step, out_type)

    def visit_RemoteCallAbstract(self, node):
        args = [self.visit(x) for x in node.args]
        if (isinstance(node.output, list)):
            outs = [self.visit(x) for x in node.output]
        else:
            outs = [self.visit(node.output)]

        if (node.kwargs is not None):
            kwargs = {k: self.visit(v) for (k,v) in node.kwargs.items()}
        else:
            kwargs = None

        return RemoteCallAbstract(node.compute, outs, args, kwargs, type)

    def visit_Mfunc(self, node):
        vals = self.visit(node.e)
        func_type = M_FUNC_OUT_TYPES[node.op]
        if (issubclass(vals.type, Const)):
            out_type = python_type_to_lp_type(func_type, const=True)
        else:
            out_type = python_type_to_lp_type(func_type)
        return Mfunc(node.op, vals, out_type)

    def visit_CmpOp(self, node):
        lhs = self.visit(node.left)
        rhs = self.visit(node.right)
        if (issubclass(lhs.type, Const) and issubclass(rhs.type, Const)):
            out_type = ConstBoolType
        else:
            out_type = BoolType
        return CmpOp(node.op, lhs, rhs, out_type)

    def visit_IntConst(self, node):
        return IntConst(node.val, ConstIntType)

    def visit_FloatConst(self, node):
        return FloatConst(node.val, ConstFloatType)

    def visit_For(self, node):
        self.decl_types[node.var] = LinearIntType
        min_idx = self.visit(node.min)
        max_idx = self.visit(node.max)
        linear_max = issubclass(min_idx.type, LinearIntType)
        linear_min = issubclass(min_idx.type, LinearIntType)
        if ((not linear_max) or (not linear_min)):
            raise LambdaPackTypeExceptions("Loop bounds must be LinearIntType")

        step = self.visit(node.step)
        body = [self.visit(x) for x in node.body]
        return For(node.var, min_idx, max_idx, step, body)

    def visit_Reduction(self, node):
        self.decl_types[node.var] = LinearIntType
        min_idx = self.visit(node.min)
        max_idx = self.visit(node.max)
        if (isinstance(node.expr, list)):
            expr = [self.visit(x) for x in node.expr]
        else:
            expr = [self.visit(node.expr)]
        for expr_i in expr:
            if (not isinstance(expr_i, IndexExpr)):
                raise LambdaPackTypeExceptions("Reduction Exprs must be of IndexExpr type")
        b_fac = self.visit(node.b_fac)
        remote_call = self.visit(node.remote_call)
        recursion = [self.visit(x) for x in node.recursion]
        return Reduction(node.var, min_idx, max_idx, expr, b_fac, remote_call, recursion)

class BackendGenerate(ast.NodeVisitor):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.global_scope = {}
        self.current_scope = self.global_scope
        self.all_symbols = {}
        self.remote_calls = {}
        self.evaluators = {}
        self.max_calls = 0
        self.count = 0
        self.arg_values = args
        self.kwargs = kwargs
        self.for_depth = 0

    def visit_FuncDef(self, node):
        assert len(node.args) == len(self.arg_values), "function {0} expected {1} args got {2}".format(node.name, len(node.args), len(self.arg_values))
        for i, (arg, arg_value, arg_type)  in enumerate(zip(node.args, self.arg_values, node.arg_types)):
            p_type = python_type_to_lp_type(type(arg_value), const=True)
            if (not issubclass(p_type, arg_type)):
                raise LambdaPackBackendGenerationException("arg {0} wrong type expected {1} got {2}".format(i, arg_type, p_type))
            self.global_scope[arg] = arg_value
            self.all_symbols[arg] = arg_value
        body = [self.visit(x) for x in node.body]
        assert(len(node.args) == len(self.arg_values))

    def visit_RemoteCallAbstract(self, node):
        reads = [self.visit(x) for x in node.args]
        writes = [self.visit(x) for x in node.output]
        self.remote_calls[self.max_calls] = RemoteCallAbstractWithContext(node, self.current_scope)
        self.max_calls += 1
        return node

    def visit_If(self, node):
        cond = self.visit(node.cond)
        prev_scope = self.current_scope
        self.current_conds.append(cond)
        if_scope = {"__parent__": prev_scope, "__condtrue__": cond}
        self.current_scope = if_scope
        if_body = [self.visit(x) for x in node.body]
        else_scope = {"__parent__": prev_scope, "__condfalse__": cond}
        self.current_scope = else_scope
        else_body = [self.visit(x) for x in node.elseBody]
        self.current_scope = prev_scope
        return node

    def visit_CmpOp(self, node):
        return node

    def visit_IndexExpr(self, node):
        return node

    def visit_Mfunc(self, node):
        return node

    def visit_BinOp(self, node):
        return node

    def visit_Assign(self, node):
        lhs = self.visit(node.lhs)
        rhs = self.visit(node.rhs)
        self.all_symbols[lhs] = rhs
        self.current_scope[str(lhs)] = rhs
        return node

    def visit_For(self, node):
        prev_scope = self.current_scope
        self.for_depth += 1
        for_loop_scope = {"__parent__": prev_scope, 'depth': self.for_depth}

        self.current_scope = for_loop_scope
        min_idx = self.visit(node.min)
        max_idx = self.visit(node.max)
        step = self.visit(node.step)
        for_loop_scope[node.var] = RangeVar(node.var, min_idx, max_idx, step)
        body = [self.visit(x) for x in node.body]
        self.for_depth -= 1
        self.current_scope = prev_scope
        return node

    def visit_Ref(self, node):
        return node.name

    def visit_UnOp(self, node):
        return node

    def visit_IntConst(self, node):
        return node

    def visit_FloatConst(self, node):
        return node

if __name__ == "__main__":
    N = 32
    nb = 8
    X = np.random.randn(N,N)
    I = BigMatrix("TSQR_input", shape=(int(N),int(N)), shard_sizes=(nb, nb))
    shard_matrix(I, X)
    Q = BigMatrix("TSQR_output_Q", shape=(int(N),int(N)), shard_sizes=(nb, nb))
    R = BigMatrix("TSQR_output_R", shape=(int(N),int(N)), shard_sizes=(nb, nb))
    program = lpcompile(TSQR)(I, Q, R, int(np.ceil(N/nb)))
    print(program)
    starters = program.starters
    print("STARTER", starters[1])
    print("TERMINATORS", program.find_terminators())
    c = program.get_children(*starters[1])
    print("starter children", c)
    c2 = program.get_children(*c[0])
    print("starter children 2", c2)


    operator_expr = program.get_expr(c[0][0])
    inst_block = operator_expr.eval_operator(c[0][1])

    operator_expr = program.get_expr(c2[0][0])
    inst_block = operator_expr.eval_operator(c2[0][1])



