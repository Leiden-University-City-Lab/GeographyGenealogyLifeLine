import os as _os
from dash.development.base_component import Component, _explicitize_args


class PersonCard(Component):
    """PersonCard — rich interactive person summary card with mini SVG timeline.

    Keyword arguments:

    - id (string; required)
    - name (string; required): Person display name.
    - faculty (string; optional): Faculty label shown as a badge.
    - color (string; default '#7f8c8d'): Hex colour for border and timeline.
    - birth_year (number; optional): Birth year integer.
    - death_year (number; optional): Death year integer.
    - events (list of dicts; default []): Events for the mini timeline.
      Each: { year (number), type (string), location (string), description (string) }
    - selected (boolean; default False): Whether the card is selected.
      Written back to Dash when the user clicks the card.
    """

    _children_props = []
    _base_nodes = ['children']
    _namespace = 'person_card'
    _type = 'PersonCard'

    # Tells Dash where to find the compiled JS bundle for this namespace.
    _js_dist = [
        {
            'relative_package_path': 'person_card.v0.1.0.min.js',
            'namespace': 'person_card',
            'async': 'eager',
        }
    ]
    _css_dist = []

    @_explicitize_args
    def __init__(
        self,
        id,
        name,
        faculty=Component.UNDEFINED,
        color=Component.UNDEFINED,
        birth_year=Component.UNDEFINED,
        death_year=Component.UNDEFINED,
        events=Component.UNDEFINED,
        selected=Component.UNDEFINED,
        **kwargs
    ):
        self._prop_names = [
            'id', 'name', 'faculty', 'color',
            'birth_year', 'death_year', 'events', 'selected',
        ]
        self._valid_wildcard_attributes = []
        self.available_properties = self._prop_names
        self.available_wildcard_properties = []

        _explicit_args = kwargs.pop('_explicit_args')
        _locals = locals()
        _locals.update(kwargs)
        args = {k: _locals[k] for k in _explicit_args}

        super(PersonCard, self).__init__(**args)
