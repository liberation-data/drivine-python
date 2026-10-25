from __future__ import annotations

import dataclasses
import functools
from typing import Any, NoReturn

from drivine.cypher.ast.clause import (
    Call,
    Clause,
    Create,
    Delete,
    Foreach,
    Match,
    Merge,
    Remove,
    Return,
    SetClause,
    Unwind,
    With,
)
from drivine.cypher.ast.expression import (
    AllProperties,
    Binary,
    BinaryOperator,
    Case,
    CountStar,
    Entry,
    Exists,
    Expression,
    Function,
    HasLabels,
    Index,
    ListComprehension,
    ListLiteral,
    Literal,
    MapLiteral,
    MapProjection,
    MapProjectionElement,
    Parameter,
    PatternComprehension,
    PatternPredicate,
    Property,
    Quantifier,
    QuantifierKind,
    Reduce,
    Selected,
    Slice,
    Subquery,
    SubqueryKind,
    Unary,
    UnaryOperator,
    Variable,
    VariableElement,
)
from drivine.cypher.ast.node_pattern import NodePattern
from drivine.cypher.ast.pattern import Pattern, Shortest
from drivine.cypher.ast.projection import Projection, ProjectionItem, SortItem
from drivine.cypher.ast.query import Query, Union
from drivine.cypher.ast.relationship_pattern import Direction, Group, Range, RelationshipPattern
from drivine.cypher.ast.remove_item import RemoveItem, RemoveLabels, RemoveProperty
from drivine.cypher.ast.set_item import SetAdd, SetItem, SetLabels, SetProperty, SetReplace
from drivine.cypher.procedure import Procedure, procedure_named
from drivine.drivine_error import DrivineError

# The parse tree's contexts are ANTLR's, generated without types: each is read as Any here.
Context = Any


@functools.lru_cache(maxsize=512)
def read_cypher(cypher: str) -> Query:
    """
    The statement, read into a Query: parsed with openCypher's grammar, then walked into the syntax tree the compiler
    works from, which says what the statement means and no more of how it was written. A statement that is not Cypher
    is refused with where it went wrong. A statement that is Cypher the compiler does not take (a call of a procedure
    it does not know) is refused by name. A call of one it knows, `CALL db.labels()`, is read as the query that gives
    its rows: see Procedure.

    ANTLR's Python runtime parses slowly, so a statement is read once: the tree is kept by its text, and is never
    changed, so each compile of the statement reads the one tree.
    """
    from antlr4 import CommonTokenStream, InputStream  # pyright: ignore[reportMissingTypeStubs]
    from antlr4.error.ErrorListener import ErrorListener  # pyright: ignore[reportMissingTypeStubs]

    from drivine.cypher.grammar.CypherLexer import CypherLexer  # pyright: ignore
    from drivine.cypher.grammar.CypherParser import CypherParser  # pyright: ignore

    class Refusing(ErrorListener):  # pyright: ignore[reportUntypedBaseClass]
        def syntaxError(self, recognizer: Any, offendingSymbol: Any, line: int, column: int, msg: str, e: Any) -> None:  # noqa: N802, N803
            raise DrivineError(f"The statement is not Cypher, at line {line}, column {column + 1}: {msg}")

    refusing = Refusing()
    lexer: Any = CypherLexer(InputStream(cypher))
    lexer.removeErrorListeners()
    lexer.addErrorListener(refusing)
    parser: Any = CypherParser(CommonTokenStream(lexer))
    parser.removeErrorListeners()
    parser.addErrorListener(refusing)
    return _Reader(CypherParser).query(parser.oC_Cypher().oC_Statement().oC_Query())


class _Reader:
    """The walk from the parse tree to the syntax tree: drivine4j's CypherReader."""

    def __init__(self, parser: Any) -> None:
        # The context classes, told apart among a rule's children by their kind.
        self.p = parser

    def query(self, context: Context) -> Query:
        regular = context.oC_RegularQuery()
        return self.regular(regular) if regular is not None else self.standalone(context.oC_StandaloneCall())

    def standalone(self, call: Context) -> Query:
        """
        `CALL db.labels()` as the whole statement: the procedure's rows, each field under the name it is yielded as, or
        its own where it yields them all, by `YIELD *` or no `YIELD`.
        """
        invocation = call.oC_ExplicitProcedureInvocation()
        name = (invocation or call.oC_ImplicitProcedureInvocation()).oC_ProcedureName()
        procedure = self.procedure(name)
        items = call.oC_YieldItems()
        # As Neo4j refuses it: a WHERE filters rows a clause after it reads.
        if items is not None and items.oC_Where() is not None:
            raise DrivineError(
                "A CALL that is the whole statement takes no WHERE, as in Cypher: follow its YIELD with WITH or "
                f"RETURN, as in CALL {procedure.qualified_name}() YIELD {procedure.field} WHERE … "
                f"RETURN {procedure.field}."
            )
        yields = self.yields(items) if items is not None else [(procedure.field, procedure.field)]
        arguments = [self.expression(it) for it in invocation.oC_Expression()] if invocation is not None else []
        return procedure.query(arguments, yields, None)

    def in_query(self, call: Context) -> Clause:
        """
        `CALL db.labels() YIELD label` within a query: for each row, the procedure's rows, joined to it as a
        `CALL { … }`'s are, each field yielded bound for the clauses after.
        """
        invocation = call.oC_ExplicitProcedureInvocation()
        procedure = self.procedure(invocation.oC_ProcedureName())
        # As Neo4j refuses it: within a query, what a procedure gives is named.
        items = call.oC_YieldItems()
        if items is None:
            raise DrivineError(
                "A CALL of a procedure within a query names what it yields, as in Cypher: "
                f"CALL {procedure.qualified_name}() YIELD {procedure.field}."
            )
        where = self.where(items.oC_Where())
        arguments = [self.expression(it) for it in invocation.oC_Expression()]
        return Call(False, procedure.query(arguments, self.yields(items), where))

    def procedure(self, name: Context) -> Procedure:
        """The procedure a call names, or the call refused where the compiler does not take it."""
        namespace = "".join(_symbolic(it) + "." for it in name.oC_Namespace().oC_SymbolicName())
        procedure = procedure_named(namespace + _symbolic(name.oC_SymbolicName()))
        if procedure is None:
            _refuse("A procedure call (CALL)")
        return procedure

    def yields(self, items: Context) -> list[tuple[str, str]]:
        """Each field a `YIELD` names, and the variable it is bound to: its own name, or the one after `AS`."""
        named: list[tuple[str, str]] = []
        for item in items.oC_YieldItem():
            variable = self.name(item.oC_Variable())
            field = item.oC_ProcedureResultField()
            named.append((_symbolic(field.oC_SymbolicName()) if field is not None else variable, variable))
        return named

    def regular(self, regular: Context) -> Query:
        return Query(
            self.single(regular.oC_SingleQuery()),
            tuple(Union(it.ALL() is not None, self.single(it.oC_SingleQuery())) for it in regular.oC_Union()),
        )

    def single(self, single: Context) -> tuple[Clause, ...]:
        clauses: list[Clause] = []
        multi = single.oC_MultiPartQuery()
        if multi is not None:
            # Its children in order: the reading clauses and updates of each part, in the order written, then the part's
            # WITH.
            for child in multi.children:
                if isinstance(child, self.p.OC_ReadingClauseContext):
                    clauses.append(self.reading(child))
                elif isinstance(child, self.p.OC_UpdatingClauseContext):
                    clauses.append(self.updating(child))
                elif isinstance(child, self.p.OC_WithContext):
                    where = child.oC_Where()
                    clauses.append(
                        With(
                            self.projection(child.oC_ProjectionBody()),
                            self.expression(where.oC_Expression()) if where is not None else None,
                        )
                    )
        last = single.oC_SinglePartQuery() if multi is None else multi.oC_SinglePartQuery()
        # Reading clauses and updates in the order written: a reading clause may follow an update.
        for child in last.children:
            if isinstance(child, self.p.OC_ReadingClauseContext):
                clauses.append(self.reading(child))
            elif isinstance(child, self.p.OC_UpdatingClauseContext):
                clauses.append(self.updating(child))
            elif isinstance(child, self.p.OC_ReturnContext):
                clauses.append(Return(self.projection(child.oC_ProjectionBody())))
        return tuple(clauses)

    def updating(self, context: Context) -> Clause:
        if context.oC_Create() is not None:
            return Create(self.patterns(context.oC_Create().oC_Pattern()))
        merge = context.oC_Merge()
        if merge is not None:
            part = merge.oC_PatternPart()
            if part.oC_AnonymousPatternPart().oC_ShortestPathPattern() is not None:
                _refuse("A shortest path in MERGE")
            actions = merge.oC_MergeAction()
            variable = part.oC_Variable()
            return Merge(
                self.pattern(
                    part.oC_AnonymousPatternPart().oC_PatternElement(), self.name(variable) if variable else None
                ),
                tuple(item for it in actions if it.CREATE() is not None for item in self.set(it.oC_Set())),
                tuple(item for it in actions if it.MATCH() is not None for item in self.set(it.oC_Set())),
            )
        if context.oC_Set() is not None:
            return SetClause(self.set(context.oC_Set()))
        if context.oC_Remove() is not None:
            return Remove(tuple(self.remove_item(it) for it in context.oC_Remove().oC_RemoveItem()))
        call = context.oC_SubqueryCall()
        if call is not None:
            return Call(call.OPTIONAL() is not None, self.regular(call.oC_RegularQuery()))
        foreach = context.oC_Foreach()
        if foreach is not None:
            return Foreach(
                self.name(foreach.oC_Variable()),
                self.expression(foreach.oC_Expression()),
                tuple(self.updating(it) for it in foreach.oC_UpdatingClause()),
            )
        delete = context.oC_Delete()
        return Delete(delete.DETACH() is not None, tuple(self.expression(it) for it in delete.oC_Expression()))

    def set(self, context: Context) -> tuple[SetItem, ...]:
        items: list[SetItem] = []
        for item in context.oC_SetItem():
            if item.oC_PropertyExpression() is not None:
                variable, key = self.property(item.oC_PropertyExpression())
                items.append(SetProperty(variable, key, self.expression(item.oC_Expression())))
            elif item.oC_NodeLabels() is not None:
                items.append(SetLabels(self.name(item.oC_Variable()), self.labels(item.oC_NodeLabels())))
            elif any(it.getText() == "+=" for it in item.children):
                items.append(SetAdd(self.name(item.oC_Variable()), self.expression(item.oC_Expression())))
            else:
                items.append(SetReplace(self.name(item.oC_Variable()), self.expression(item.oC_Expression())))
        return tuple(items)

    def remove_item(self, context: Context) -> RemoveItem:
        if context.oC_NodeLabels() is not None:
            return RemoveLabels(self.name(context.oC_Variable()), self.labels(context.oC_NodeLabels()))
        variable, key = self.property(context.oC_PropertyExpression())
        return RemoveProperty(variable, key)

    def property(self, context: Context) -> tuple[str, str]:
        """`n.key`, as a variable and a key: a property of anything else, or of a property, is not written to."""
        variable = context.oC_Atom().oC_Variable()
        if variable is None or len(context.oC_PropertyLookup()) != 1:
            _refuse("A write to a property of anything but a variable")
        return self.name(variable), self.schema_name(context.oC_PropertyLookup(0).oC_PropertyKeyName().oC_SchemaName())

    def labels(self, context: Context) -> tuple[str, ...]:
        return tuple(self.schema_name(it.oC_LabelName().oC_SchemaName()) for it in context.oC_NodeLabel())

    def reading(self, context: Context) -> Clause:
        call = context.oC_SubqueryCall()
        if call is not None:
            return Call(call.OPTIONAL() is not None, self.regular(call.oC_RegularQuery()))
        match = context.oC_Match()
        if match is not None:
            return Match(match.OPTIONAL() is not None, self.patterns(match.oC_Pattern()), self.where(match.oC_Where()))
        unwind = context.oC_Unwind()
        if unwind is not None:
            return Unwind(self.expression(unwind.oC_Expression()), self.name(unwind.oC_Variable()))
        return self.in_query(context.oC_InQueryCall())

    def readings(self, context: Context) -> Query:
        """Reading clauses that return nothing, the body of an `EXISTS { … }` or a `COUNT { … }`."""
        return Query(tuple(self.reading(it) for it in context.oC_ReadingClause()))

    def where(self, context: Context | None) -> Expression | None:
        return self.expression(context.oC_Expression()) if context is not None else None

    def projection(self, context: Context) -> Projection:
        items = context.oC_ProjectionItems()
        order = context.oC_Order()
        skip = context.oC_Skip()
        limit = context.oC_Limit()
        sorts: list[Context] = order.oC_SortItem() if order is not None else []
        return Projection(
            distinct=context.DISTINCT() is not None,
            star=bool(items.children) and items.children[0].getText() == "*",
            items=tuple(
                ProjectionItem(
                    self.expression(item.oC_Expression()),
                    self.name(item.oC_Variable()) if item.oC_Variable() is not None else item.oC_Expression().getText(),
                )
                for item in items.oC_ProjectionItem()
            ),
            order_by=tuple(
                SortItem(
                    self.expression(sort.oC_Expression()), sort.DESC() is not None or sort.DESCENDING() is not None
                )
                for sort in sorts
            ),
            skip=self.expression(skip.oC_Expression()) if skip is not None else None,
            limit=self.expression(limit.oC_Expression()) if limit is not None else None,
        )

    # ----- Patterns -----

    def patterns(self, context: Context) -> tuple[Pattern, ...]:
        patterns: list[Pattern] = []
        for part in context.oC_PatternPart():
            variable = self.name(part.oC_Variable()) if part.oC_Variable() is not None else None
            anonymous = part.oC_AnonymousPatternPart()
            shortest = anonymous.oC_ShortestPathPattern()
            if shortest is not None:
                found = self.pattern(shortest.oC_PatternElement(), variable)
                kind = Shortest.ONE if shortest.SHORTESTPATH() is not None else Shortest.ALL
                patterns.append(Pattern(found.variable, found.nodes, found.relationships, kind))
            else:
                patterns.append(self.pattern(anonymous.oC_PatternElement(), variable))
        return tuple(patterns)

    def pattern(self, context: Context, variable: str | None) -> Pattern:
        # A pattern in parentheses is the pattern.
        inner = context.oC_PatternElement()
        if inner is not None:
            return self.pattern(inner, variable)
        nodes: list[NodePattern] = []
        relationships: list[RelationshipPattern] = []
        # A quantified path pattern with no node beside it is joined to a node of no label of its own.
        for child in context.children:
            if isinstance(child, self.p.OC_NodePatternContext):
                if len(nodes) > len(relationships):
                    raise DrivineError(
                        "Two nodes of a path are side by side: a relationship or a quantified path pattern goes "
                        "between them."
                    )
                nodes.append(self.node(child))
            elif isinstance(child, self.p.OC_PatternElementChainContext):
                # Only after a quantified path pattern: Cypher puts a node between them.
                if len(nodes) == len(relationships):
                    raise DrivineError("A relationship after a quantified path pattern has a node between them.")
                relationships.append(self.chained(child))
                nodes.append(self.node(child.oC_NodePattern()))
            elif isinstance(child, self.p.OC_QuantifiedPathPatternContext):
                if len(nodes) == len(relationships):
                    nodes.append(NodePattern(None, (), None))
                relationships.append(self.quantified(child))
        if len(nodes) == len(relationships):
            nodes.append(NodePattern(None, (), None))
        return Pattern(variable, tuple(nodes), tuple(relationships))

    def quantified(self, context: Context) -> RelationshipPattern:
        """
        `((a)-[:R]->(b) WHERE …){1,3}`: a relationship of the pattern that repeats its own, between the nodes beside
        it.
        """
        pattern = self.pattern(context.oC_PatternElement(), None)
        if not pattern.relationships:
            raise DrivineError("A quantified path pattern holds a relationship.")
        for relationship in pattern.relationships:
            if relationship.group is not None:
                raise DrivineError("A quantified path pattern holds no quantified path pattern.")
            if relationship.range is not None:
                _refuse("A variable-length relationship in a quantified path pattern")
        where = context.oC_Where()
        group = Group(pattern, self.expression(where.oC_Expression()) if where is not None else None)
        range_ = self.quantifier_range(context.oC_PathQuantifier())
        # As in Neo4j: a group that may repeat no times at most matches nothing of its own.
        if range_.max == 0:
            raise DrivineError("A quantified path pattern's quantifier has a maximum of at least 1.")
        return RelationshipPattern(None, (), Direction.OUTGOING, range_, None, group)

    def relationships_pattern(self, context: Context, variable: str | None) -> Pattern:
        nodes = [self.node(context.oC_NodePattern())]
        relationships: list[RelationshipPattern] = []
        for chain in context.oC_PatternElementChain():
            relationships.append(self.chained(chain))
            nodes.append(self.node(chain.oC_NodePattern()))
        return Pattern(variable, tuple(nodes), tuple(relationships))

    def chained(self, chain: Context) -> RelationshipPattern:
        """The relationship of a link of a path, `-[:R]->` or a quantified relationship, `-[:R]->{1,3}`."""
        relationship = self.relationship(chain.oC_RelationshipPattern())
        quantifier = chain.oC_PathQuantifier()
        if quantifier is None:
            return relationship
        if relationship.range is not None:
            _refuse("A quantified relationship that has a range of its own")
        return dataclasses.replace(relationship, range=self.quantifier_range(quantifier))

    def quantifier_range(self, context: Context) -> Range:
        """
        `+` is one or more, `*` any, `{n}` exactly n, and `{m,n}` from m to n, either bound left out. As in Neo4j,
        braces with no bound in them, `{}`, and a least bound above the greatest, `{3,1}`, are refused.
        """
        text = context.getText()
        if text == "+":
            return Range(1, None)
        if text == "*":
            return Range(0, None)
        integers = context.oC_IntegerLiteral()
        children = list(context.children)
        comma = next((i for i, it in enumerate(children) if it.getText() == ","), -1)
        if comma < 0:
            if len(integers) != 1:
                raise DrivineError("A quantifier in braces has a bound in them, as in {2} or {1,3}.")
            bound = _integer(integers[0])
            return Range(bound, bound)
        before = next((_integer(it) for it in integers if children.index(it) < comma), None)
        after = next((_integer(it) for it in integers if children.index(it) > comma), None)
        if before is not None and after is not None and before > after:
            raise DrivineError(
                f"A quantifier's least bound is at most its greatest, and {before} is more than {after}."
            )
        return Range(before if before is not None else 0, after)

    def node(self, context: Context) -> NodePattern:
        variable = context.oC_Variable()
        labels = context.oC_NodeLabels()
        properties = context.oC_Properties()
        return NodePattern(
            self.name(variable) if variable is not None else None,
            self.labels(labels) if labels is not None else (),
            self.properties(properties) if properties is not None else None,
        )

    def relationship(self, context: Context) -> RelationshipPattern:
        left = context.oC_LeftArrowHead() is not None
        right = context.oC_RightArrowHead() is not None
        detail = context.oC_RelationshipDetail()
        if right and not left:
            direction = Direction.OUTGOING
        elif left and not right:
            direction = Direction.INCOMING
        else:
            direction = Direction.EITHER
        if detail is None:
            return RelationshipPattern(None, (), direction, None, None)
        variable = detail.oC_Variable()
        types = detail.oC_RelationshipTypes()
        range_ = detail.oC_RangeLiteral()
        properties = detail.oC_Properties()
        return RelationshipPattern(
            self.name(variable) if variable is not None else None,
            tuple(self.schema_name(it.oC_SchemaName()) for it in types.oC_RelTypeName()) if types is not None else (),
            direction,
            self.range_literal(range_) if range_ is not None else None,
            self.properties(properties) if properties is not None else None,
        )

    def range_literal(self, context: Context) -> Range:
        """`*`, `*2`, `*1..3`, `*..3`, `*2..`: a bare number is both bounds."""
        integers = context.oC_IntegerLiteral()
        bounds = [_integer(it) for it in integers]
        children = list(context.children)
        dots = any(it.getText() == ".." for it in children)
        if not dots and len(bounds) == 1:
            return Range(bounds[0], bounds[0])
        if not dots:
            return Range(None, None)
        if len(bounds) == 2:
            return Range(bounds[0], bounds[1])
        if not bounds:
            return Range(None, None)
        # One bound and the dots: before them it is the least, after them the most.
        if next(i for i, it in enumerate(children) if it.getText() == "..") < children.index(integers[0]):
            return Range(None, bounds[0])
        return Range(bounds[0], None)

    def properties(self, context: Context) -> Expression:
        map_ = context.oC_MapLiteral()
        return self.map(map_) if map_ is not None else self.parameter(context.oC_Parameter())

    # ----- Expressions -----

    def expression(self, context: Context) -> Expression:
        return self.or_(context.oC_OrExpression())

    def or_(self, context: Context) -> Expression:
        return _fold([self.xor(it) for it in context.oC_XorExpression()], BinaryOperator.OR)

    def xor(self, context: Context) -> Expression:
        return _fold([self.and_(it) for it in context.oC_AndExpression()], BinaryOperator.XOR)

    def and_(self, context: Context) -> Expression:
        return _fold([self.not_(it) for it in context.oC_NotExpression()], BinaryOperator.AND)

    def not_(self, context: Context) -> Expression:
        result = self.comparison(context.oC_ComparisonExpression())
        for _ in context.NOT():
            result = Unary(UnaryOperator.NOT, result)
        return result

    def comparison(self, context: Context) -> Expression:
        """`a < b <= c` is `a < b AND b <= c`, as Cypher reads a chain of comparisons."""
        left = self.predicate(context.oC_StringListNullPredicateExpression())
        result: Expression | None = None
        for partial in context.oC_PartialComparisonExpression():
            right = self.predicate(partial.oC_StringListNullPredicateExpression())
            operator = _COMPARISONS[partial.children[0].getText()]
            one = Binary(operator, left, right)
            result = Binary(BinaryOperator.AND, result, one) if result is not None else one
            left = right
        return result if result is not None else left

    def predicate(self, context: Context) -> Expression:
        result = self.additive(context.oC_AddOrSubtractExpression())
        for child in context.children[1:]:
            if isinstance(child, self.p.OC_StringPredicateExpressionContext):
                if child.STARTS() is not None:
                    operator = BinaryOperator.STARTS_WITH
                elif child.ENDS() is not None:
                    operator = BinaryOperator.ENDS_WITH
                elif child.CONTAINS() is not None:
                    operator = BinaryOperator.CONTAINS
                else:
                    operator = BinaryOperator.MATCHES
                result = Binary(operator, result, self.additive(child.oC_AddOrSubtractExpression()))
            elif isinstance(child, self.p.OC_ListPredicateExpressionContext):
                result = Binary(BinaryOperator.IN, result, self.additive(child.oC_AddOrSubtractExpression()))
            elif isinstance(child, self.p.OC_NullPredicateExpressionContext):
                result = Unary(UnaryOperator.IS_NOT_NULL if child.NOT() is not None else UnaryOperator.IS_NULL, result)
        return result

    def additive(self, context: Context) -> Expression:
        operands = [self.multiplicative(it) for it in context.oC_MultiplyDivideModuloExpression()]
        operators = [it.getText() for it in context.children if it.getText() in ("+", "-")]
        result = operands[0]
        for right, operator in zip(operands[1:], operators, strict=False):
            result = Binary(BinaryOperator.ADD if operator == "+" else BinaryOperator.SUBTRACT, result, right)
        return result

    def multiplicative(self, context: Context) -> Expression:
        operands = [self.power(it) for it in context.oC_PowerOfExpression()]
        operators = [it.getText() for it in context.children if it.getText() in ("*", "/", "%")]
        result = operands[0]
        for right, operator in zip(operands[1:], operators, strict=False):
            result = Binary(_MULTIPLICATIVE[operator], result, right)
        return result

    def power(self, context: Context) -> Expression:
        return _fold([self.unary(it) for it in context.oC_UnaryAddOrSubtractExpression()], BinaryOperator.POWER)

    def unary(self, context: Context) -> Expression:
        negated = context.children[0].getText() == "-"
        written = context.oC_NonArithmeticOperatorExpression()
        # The least whole number, -9223372036854775808, is written negated: its digits alone are past the greatest.
        if negated and written.getChildCount() == 1:
            literal = written.oC_Atom().oC_Literal()
            number = literal.oC_NumberLiteral() if literal is not None else None
            integer = number.oC_IntegerLiteral() if number is not None else None
            if integer is not None:
                magnitude = _magnitude(integer)
                if magnitude == _LONG_MIN_MAGNITUDE:
                    return Literal(-_LONG_MIN_MAGNITUDE)
                if magnitude > _LONG_MIN_MAGNITUDE:
                    raise _out_of_range(f"-{integer.getText()}")
        operand = self.non_arithmetic(written)
        return Unary(UnaryOperator.NEGATE, operand) if negated else operand

    def non_arithmetic(self, context: Context) -> Expression:
        """An atom, then its property lookups and list operators in the order written, then a label test."""
        result = self.atom(context.oC_Atom())
        for child in context.children[1:]:
            if isinstance(child, self.p.OC_PropertyLookupContext):
                result = Property(result, self.schema_name(child.oC_PropertyKeyName().oC_SchemaName()))
            elif isinstance(child, self.p.OC_ListOperatorExpressionContext):
                children = list(child.children)
                if not any(it.getText() == ".." for it in children):
                    result = Index(result, self.expression(child.oC_Expression(0)))
                else:
                    # Which bound each expression is: before the dots, the start; after them, the end.
                    split = next(i for i, it in enumerate(children) if it.getText() == "..")
                    bounds = child.oC_Expression()
                    from_ = next((self.expression(it) for it in bounds if children.index(it) < split), None)
                    to = next((self.expression(it) for it in bounds if children.index(it) > split), None)
                    result = Slice(result, from_, to)
            elif isinstance(child, self.p.OC_NodeLabelsContext):
                result = HasLabels(result, self.labels(child))
        return result

    def atom(self, context: Context) -> Expression:
        if context.oC_Literal() is not None:
            return self.literal(context.oC_Literal())
        if context.oC_Parameter() is not None:
            return self.parameter(context.oC_Parameter())
        if context.oC_CaseExpression() is not None:
            return self.case(context.oC_CaseExpression())
        if context.COUNT() is not None:
            return CountStar()
        comprehension = context.oC_ListComprehension()
        if comprehension is not None:
            filter_ = comprehension.oC_FilterExpression()
            projected = comprehension.oC_Expression()
            return ListComprehension(
                self.name(filter_.oC_IdInColl().oC_Variable()),
                self.expression(filter_.oC_IdInColl().oC_Expression()),
                self.where(filter_.oC_Where()),
                self.expression(projected) if projected is not None else None,
            )
        pattern_comprehension = context.oC_PatternComprehension()
        if pattern_comprehension is not None:
            variable = pattern_comprehension.oC_Variable()
            return PatternComprehension(
                self.relationships_pattern(
                    pattern_comprehension.oC_RelationshipsPattern(), self.name(variable) if variable else None
                ),
                self.where(pattern_comprehension.oC_Where()),
                self.expression(pattern_comprehension.oC_Expression()),
            )
        quantifier = context.oC_Quantifier()
        if quantifier is not None:
            if quantifier.ALL() is not None:
                kind = QuantifierKind.ALL
            elif quantifier.ANY() is not None:
                kind = QuantifierKind.ANY
            elif quantifier.NONE() is not None:
                kind = QuantifierKind.NONE
            else:
                kind = QuantifierKind.SINGLE
            filter_ = quantifier.oC_FilterExpression()
            return Quantifier(
                kind,
                self.name(filter_.oC_IdInColl().oC_Variable()),
                self.expression(filter_.oC_IdInColl().oC_Expression()),
                self.where(filter_.oC_Where()),
            )
        reduce = context.oC_Reduce()
        if reduce is not None:
            return Reduce(
                self.name(reduce.oC_Variable()),
                self.expression(reduce.oC_Expression(0)),
                self.name(reduce.oC_IdInColl().oC_Variable()),
                self.expression(reduce.oC_IdInColl().oC_Expression()),
                self.expression(reduce.oC_Expression(1)),
            )
        if context.oC_PatternPredicate() is not None:
            return PatternPredicate(
                self.relationships_pattern(context.oC_PatternPredicate().oC_RelationshipsPattern(), None)
            )
        if context.oC_ParenthesizedExpression() is not None:
            return self.expression(context.oC_ParenthesizedExpression().oC_Expression())
        function = context.oC_FunctionInvocation()
        if function is not None:
            namespace = "".join(
                _symbolic(it) + "." for it in function.oC_FunctionName().oC_Namespace().oC_SymbolicName()
            )
            return Function(
                namespace + _symbolic(function.oC_FunctionName().oC_SymbolicName()),
                function.DISTINCT() is not None,
                tuple(self.expression(it) for it in function.oC_Expression()),
            )
        exists = context.oC_ExistentialSubquery()
        if exists is not None:
            if exists.oC_RegularQuery() is not None:
                return Subquery(SubqueryKind.EXISTS, self.regular(exists.oC_RegularQuery()))
            if exists.oC_SubqueryReadings() is not None:
                return Subquery(SubqueryKind.EXISTS, self.readings(exists.oC_SubqueryReadings()))
            return Exists(self.patterns(exists.oC_Pattern()), self.where(exists.oC_Where()))
        count = context.oC_CountSubquery()
        if count is not None:
            if count.oC_RegularQuery() is not None:
                query = self.regular(count.oC_RegularQuery())
            elif count.oC_SubqueryReadings() is not None:
                query = self.readings(count.oC_SubqueryReadings())
            else:
                query = Query((Match(False, self.patterns(count.oC_Pattern()), self.where(count.oC_Where())),))
            return Subquery(SubqueryKind.COUNT, query)
        projection = context.oC_MapProjection()
        if projection is not None:
            return MapProjection(
                self.name(projection.oC_Variable()),
                tuple(self.map_projection_element(it) for it in projection.oC_MapProjectionElement()),
            )
        return Variable(self.name(context.oC_Variable()))

    def map_projection_element(self, element: Context) -> MapProjectionElement:
        if element.oC_Expression() is not None:
            return Entry(
                self.schema_name(element.oC_PropertyKeyName().oC_SchemaName()), self.expression(element.oC_Expression())
            )
        if element.oC_PropertyKeyName() is not None:
            return Selected(self.schema_name(element.oC_PropertyKeyName().oC_SchemaName()))
        if element.oC_Variable() is not None:
            return VariableElement(self.name(element.oC_Variable()))
        return AllProperties()

    def case(self, context: Context) -> Expression:
        expressions = context.oC_Expression()
        children = list(context.children)
        # A subject is an expression before the first WHEN; ELSE's is after the last alternative.
        first_when = children.index(context.oC_CaseAlternative(0))
        subject = next((self.expression(it) for it in expressions if children.index(it) < first_when), None)
        otherwise = next((self.expression(it) for it in expressions if children.index(it) > first_when), None)
        return Case(
            subject,
            tuple(
                (self.expression(it.oC_Expression(0)), self.expression(it.oC_Expression(1)))
                for it in context.oC_CaseAlternative()
            ),
            otherwise,
        )

    def literal(self, context: Context) -> Expression:
        if context.oC_BooleanLiteral() is not None:
            return Literal(context.oC_BooleanLiteral().TRUE() is not None)
        if context.NULL() is not None:
            return Literal(None)
        number = context.oC_NumberLiteral()
        if number is not None:
            integer = number.oC_IntegerLiteral()
            return Literal(_integer(integer) if integer is not None else float(number.oC_DoubleLiteral().getText()))
        if context.StringLiteral() is not None:
            return Literal(_string(context.StringLiteral().getText()))
        if context.oC_ListLiteral() is not None:
            return ListLiteral(tuple(self.expression(it) for it in context.oC_ListLiteral().oC_Expression()))
        return self.map(context.oC_MapLiteral())

    def map(self, context: Context) -> MapLiteral:
        return MapLiteral(
            tuple(
                (self.schema_name(key.oC_SchemaName()), self.expression(value))
                for key, value in zip(context.oC_PropertyKeyName(), context.oC_Expression(), strict=False)
            )
        )

    def parameter(self, context: Context) -> Parameter:
        """`$name`, `$1`, or a keyword, `$limit`, which names a parameter as any other word does."""
        schema = context.oC_SchemaName()
        if schema is not None:
            return Parameter(self.schema_name(schema))
        number = context.DecimalInteger()
        return Parameter(number.getText() if number is not None else context.getChild(1).getText())

    # ----- Names -----

    def name(self, context: Context) -> str:
        return _symbolic(context.oC_SymbolicName())

    def schema_name(self, context: Context) -> str:
        """A name, as written or between backticks, where a doubled backtick is one."""
        symbolic = context.oC_SymbolicName()
        return _symbolic(symbolic) if symbolic is not None else context.oC_ReservedWord().getText()


_COMPARISONS = {
    "=": BinaryOperator.EQUALS,
    "<>": BinaryOperator.NOT_EQUALS,
    "<": BinaryOperator.LESS_THAN,
    ">": BinaryOperator.GREATER_THAN,
    "<=": BinaryOperator.LESS_THAN_OR_EQUAL,
    ">=": BinaryOperator.GREATER_THAN_OR_EQUAL,
}

_MULTIPLICATIVE = {"*": BinaryOperator.MULTIPLY, "/": BinaryOperator.DIVIDE, "%": BinaryOperator.MODULO}


def _fold(operands: list[Expression], operator: BinaryOperator) -> Expression:
    """The operands joined by the operator, from the left."""
    return functools.reduce(lambda a, b: Binary(operator, a, b), operands)


def _symbolic(context: Context) -> str:
    text: str = context.getText()
    return text[1:-1].replace("``", "`") if text.startswith("`") else text


def _integer(context: Context) -> int:
    """The whole number an integer literal writes, refused past the greatest 64-bit integer."""
    magnitude = _magnitude(context)
    if magnitude > _LONG_MAX_MAGNITUDE:
        raise _out_of_range(context.getText())
    return magnitude


def _magnitude(context: Context) -> int:
    """
    The number an integer literal writes, decimal, hexadecimal (`0x`) or octal (`0o`, or a leading 0), however large.
    """
    text: str = context.getText()
    if text.startswith("0x"):
        return int(text[2:], 16)
    if text.startswith("0o"):
        return int(text[2:], 8)
    if len(text) > 1 and text.startswith("0"):
        return int(text[1:], 8)
    return int(text)


def _out_of_range(written: str) -> DrivineError:
    return DrivineError(
        f"{written} is past the range of a whole number, from -9223372036854775808 to 9223372036854775807."
    )


_LONG_MAX_MAGNITUDE = 2**63 - 1
_LONG_MIN_MAGNITUDE = 2**63

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", "b": "\b", "f": "\f"}


def _string(text: str) -> str:
    """A string literal's value: its quotes taken off and its escapes read."""
    body = text[1:-1]
    out: list[str] = []
    i = 0
    while i < len(body):
        c = body[i]
        if c != "\\" or i == len(body) - 1:
            out.append(c)
            i += 1
            continue
        escaped = body[i + 1]
        if escaped in ("u", "U"):
            digits = 4 if escaped == "u" else 8
            out.append(chr(int(body[i + 2 : i + 2 + digits], 16)))
            i += digits
        else:
            out.append(_ESCAPES.get(escaped, escaped))
        i += 2
    # A character past the Basic Multilingual Plane written as two escapes, a surrogate pair, is one character.
    return "".join(out).encode("utf-16", "surrogatepass").decode("utf-16")


def _refuse(construct: str) -> NoReturn:
    """Cypher the compiler does not take: refused by name, so the caller is told what, and not that the SQL failed."""
    raise DrivineError(f"{construct} is Cypher, and is not compiled to SQL yet.")
