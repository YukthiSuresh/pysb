#!/usr/bin/env python
"""
Usage
=====

Usage: ``python -m pysb.tools.render_reactions mymodel.py > mymodel.dot``

If your model uses species as expression rates, you can visualize
these interactions by including the --include-rate-species option::
    python -m pysb.tools.render_reactions --include-rate-species mymodel.py > mymodel.dot

Renders the reactions produced by a model into the "dot" graph format which can
be visualized with Graphviz.

To create a PDF from the .dot file, use the "dot" command from Graphviz::

    dot mymodel.dot -T pdf -O

This will create mymodel.dot.pdf. You can also change the "dot" command to one
of the other Graphviz drawing tools for a different type of layout. Note that
you can pipe the output of render_reactions straight into Graphviz without
creating an intermediate .dot file, which is especially helpful if you are
making continuous changes to the model and need to visualize your changes
repeatedly::

    python -m pysb.tools.render_reactions mymodel.py | dot -T pdf -o mymodel.pdf

Note that some PDF viewers will auto-reload a changed PDF, so you may not even
need to manually reopen it every time you rerun the tool.

Output for Robertson example model
==================================

The Robertson example model (in ``pysb.examples.robertson``) contains
the following three reactions:

* A -> B
* B + B -> B + C
* C + B -> C + A

The reaction network diagram for this system as generated by this module and
rendered using ``dot`` is shown below:

.. image:: robertson_reactions.png
   :align: center
   :alt: Reaction network for pysb.examples.robertson

Circular nodes (``r0``, ``r1`` and ``r2``) indicate reactions; square nodes
(``A()``, ``B()`` and ``C()``) indicate species. Incoming arrows from a species
node to a reaction node indicate that the species is a reactant; outgoing
arrows from a reaction node to a species node indicate that the species is a
product. A hollow diamond-tipped arrow from a species to a reaction indicates
that the species is involved as both a reactant and a product, i.e., it serves
as a "modifier" (enzyme or catalyst).

"""

import pysb
import pysb.bng
import re
import sys
import os
try:
    import pygraphviz
except ImportError:
    pygraphviz = None


def run(model, include_rate_species=False):
    """
    Render the reactions produced by a model into the "dot" graph format.

    Parameters
    ----------
    model : pysb.core.Model
        The model to render.
    include_rate_species : bool
        If True, enable multigraph and add dashed edges from species used in
         expression rates to the node representing the reaction.

    Returns
    -------
    string
        The dot format output.
    """
    if pygraphviz is None:
        raise ImportError('pygraphviz library is required to run this '
                          'function')

    pysb.bng.generate_equations(model)
    # Enable multigraph when include_rate_species is True
    strict = True
    if include_rate_species:
        strict = False

    graph = pygraphviz.AGraph(directed=True, rankdir="LR", strict=strict)
    ic_species = [ic.pattern for ic in model.initials]
    for i, cp in enumerate(model.species):
        species_node = 's%d' % i
        slabel = re.sub(r'% ', r'%\\l', str(cp))
        slabel += '\\l'
        color = "#ccffcc"
        # color species with an initial condition differently
        if len([s for s in ic_species if s.is_equivalent_to(cp)]):
            color = "#aaffff"
        graph.add_node(species_node,
                       label=slabel,
                       shape="Mrecord",
                       fillcolor=color, style="filled", color="transparent",
                       fontsize="12",
                       margin="0.06,0")
    for i, reaction in enumerate(model.reactions_bidirectional):
        reaction_node = 'r%d' % i
        graph.add_node(reaction_node,
                       label=reaction_node,
                       shape="circle",
                       fillcolor="lightgray", style="filled", color="transparent",
                       fontsize="12",
                       width=".3", height=".3", margin="0.06,0")
        reactants = set(reaction['reactants'])
        products = set(reaction['products'])
        modifiers = reactants & products
        reactants = reactants - modifiers
        products = products - modifiers
        attr_reversible = {'dir': 'both', 'arrowtail': 'empty'} if reaction['reversible'] else {}

        rule = model.rules.get(reaction['rule'][0])
        # Add a dashed edge when reaction forward and/or reverse parameters are
        # expressions that contain observables
        if include_rate_species:
            sps_forward = set()
            if isinstance(rule.rate_forward, pysb.core.Expression):
                sps_forward = sp_from_expression(rule.rate_forward)
                for s in sps_forward:
                    r_link(graph, s, i, **{'style': 'dashed'})

            if isinstance(rule.rate_reverse, pysb.core.Expression):
                sps_reverse = sp_from_expression(rule.rate_reverse)
                # Don't add edges that were added with forward parameters
                sps_reverse = sps_reverse - sps_forward
                for s in sps_reverse:
                    r_link(graph, s, i, **{'style': 'dashed'})

        for s in reactants:
            r_link(graph, s, i, **attr_reversible)
        for s in products:
            r_link(graph, s, i, _flip=True, **attr_reversible)
        for s in modifiers:
            r_link(graph, s, i, arrowhead="odiamond")
    return graph.string()


def r_link(graph, s, r, **attrs):
    nodes = ('s%d' % s, 'r%d' % r)
    if attrs.get('_flip'):
        del attrs['_flip']
        nodes = reversed(nodes)
    attrs.setdefault('arrowhead', 'normal')
    graph.add_edge(*nodes, **attrs)


def sp_from_expression(expression):
    expr_sps = []
    for a in expression.expr.atoms():
        if isinstance(a, pysb.core.Observable):
            sps = a.species
            expr_sps += sps
    return set(expr_sps)

usage = __doc__
usage = usage[1:]  # strip leading newline

if __name__ == '__main__':
    # sanity checks on filename
    if len(sys.argv) <= 1:
        print(usage, end=' ')
        exit()
    model_filename = sys.argv[-1]
    if not os.path.exists(model_filename):
        raise Exception("File '%s' doesn't exist" % model_filename)
    if not re.search(r'\.py$', model_filename):
        raise Exception("File '%s' is not a .py file" % model_filename)
    sys.path.insert(0, os.path.dirname(model_filename))
    model_name = re.sub(r'\.py$', '', os.path.basename(model_filename))
    # import it
    try:
        # FIXME if the model has the same name as some other "real" module
        # which we use, there will be trouble
        # (use the imp package and import as some safe name?)
        model_module = __import__(model_name)
    except Exception as e:
        print("Error in model script:\n")
        raise
    # grab the 'model' variable from the module
    try:
        model = model_module.__dict__['model']
    except KeyError:
        raise Exception("File '%s' isn't a model file" % model_filename)
    include_rate_species = False
    if '--include-rate-species' in sys.argv:
        include_rate_species = True
    print(run(model, include_rate_species))



