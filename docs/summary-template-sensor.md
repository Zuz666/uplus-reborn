# Summary template sensor

An optional Home Assistant `template:` sensor (`sensor.hwd120_b1558u_summary`)
that combines both drums' authoritative **U+ Reborn** read-only entities into
one compact summary (`≤ 255` characters) plus structured `upper` and `lower`
state attributes.

This template reads only authoritative appliance status entities — it never
reads local next-run drafts or duplicates cumulative consumption counters.

- [English template sensor](#english-template-sensor)
- [Russian template sensor](#russian-template-sensor)
- [Optional dashboard Markdown card](#optional-dashboard-markdown-card)
- [Optional Recorder exclusion](#optional-recorder-exclusion)

## Before you add it

- **Entity prefix (`hwd120_b1558u_`):** Home Assistant always generates English
  entity suffixes (`_power`, `_upper_state`, `_upper_programme_now`,
  `_upper_water_plus_on`, and so on) on **both** English and Russian Home
  Assistant servers. Only change `hwd120_b1558u_` if you renamed the device or
  added it under a custom title (check the `Power` binary sensor entity ID on
  the **U+ Reborn** device page).
- **Optional switches:** The `child_lock` and `buzzer` attributes read
  `switch.<prefix>child_lock` and `switch.<prefix>sound`. When remote controls
  are disabled in integration settings, those two attributes evaluate to
  `null`.
- **Installation:** Add the chosen `template:` block below to
  `configuration.yaml` (or omit the outer `template:` key if placing it inside
  a separate `template: !include ...` file), run **Developer tools → YAML →
  Check configuration**, and reload **Template entities** (no Home Assistant
  Core restart required). Requires Home Assistant `2026.2.3` or newer.

> [!NOTE]
> `state_translated()` outputs labels in Home Assistant's **server language**
> (**Settings → System → General**), not per-user browser language. Use the
> [English template sensor](#english-template-sensor) on an English server and
> the [Russian template sensor](#russian-template-sensor) on a Russian server so
> programme, phase, and fault labels match the rest of the summary text.

## English template sensor

Use this configuration when your Home Assistant server language is English.
Replace `hwd120_b1558u_` if your washer entities use a different device prefix.

```yaml
template:
  - sensor:
      - name: "Washer summary"
        unique_id: hwd120_b1558u_summary
        default_entity_id: sensor.hwd120_b1558u_summary
        availability: >-
          {{ has_value('binary_sensor.hwd120_b1558u_power') }}
        icon: >-
          {% set p = 'binary_sensor.hwd120_b1558u_' %}
          {% if is_state(p ~ 'upper_fault', 'on') or is_state(p ~ 'lower_fault', 'on') %}
            mdi:washing-machine-alert
          {% elif is_state(p ~ 'power', 'off') %}
            mdi:washing-machine-off
          {% else %}
            mdi:washing-machine
          {% endif %}
        state: >-
          {%- set p = 'hwd120_b1558u_' -%}
          {%- macro drum(d, label) -%}
            {%- set s = 'sensor.' ~ p ~ d ~ '_' -%}
            {%- set b = 'binary_sensor.' ~ p ~ d ~ '_' -%}
            {%- set st = states(s ~ 'state') -%}
            {%- set phase = states(s ~ 'phase') -%}
            {%- set parts = namespace(items=[]) -%}
            {%- if is_state(b ~ 'fault', 'on') -%}
              {%- set code = states(s ~ 'fault_code') -%}
              {%- set parts.items = ['⚠ ' ~ (state_translated(s ~ 'fault_code') if code not in ['none', 'unknown', 'unavailable'] else 'Fault')] -%}
            {%- endif -%}
            {%- if st in ['running', 'paused', 'delayed', 'completed'] and states(s ~ 'programme_now') not in ['none', 'unknown', 'unavailable'] -%}
              {%- set parts.items = parts.items + [state_translated(s ~ 'programme_now')] -%}
            {%- endif -%}
            {%- if st in ['paused', 'delayed', 'completed'] -%}
              {%- set label_st = {'paused': 'Paused', 'delayed': 'Delayed', 'completed': 'Completed'}[st] -%}
              {%- set parts.items = parts.items + [label_st ~ (' 🔒' if st == 'completed' and is_state(b ~ 'door_lock', 'on') else '')] -%}
            {%- elif st == 'running' -%}
              {%- set parts.items = parts.items + [state_translated(s ~ 'phase') if phase not in ['none', 'standby', 'unknown', 'unavailable'] else 'Running'] -%}
            {%- endif -%}
            {%- if st in ['running', 'paused', 'delayed'] -%}
              {%- set temp = states(s ~ 'target_temp') -%}
              {%- set rem = states(s ~ 'time_left') -%}
              {%- if is_number(temp) -%}{%- set parts.items = parts.items + [(temp | int) ~ '°C'] -%}{%- endif -%}
              {%- if is_number(rem) -%}
                {%- set m = rem | int -%}
                {%- if m >= 60 -%}
                  {%- set parts.items = parts.items + [m // 60 ~ ' h' ~ (' ' ~ (m % 60) ~ ' min' if m % 60 else '')] -%}
                {%- else -%}
                  {%- set parts.items = parts.items + [m ~ ' min'] -%}
                {%- endif -%}
              {%- endif -%}
            {%- endif -%}
            {{- label ~ ': ' ~ parts.items | join(' · ') if parts.items else '' -}}
          {%- endmacro -%}
          {%- set upper = drum('upper', 'Upper') -%}
          {%- set lower = drum('lower', 'Lower') -%}
          {%- set readings = [upper, lower] | reject('equalto', '') | list -%}
          {%- set b = 'binary_sensor.' ~ p -%}
          {%- set s = 'sensor.' ~ p -%}
          {%- set fault = is_state(b ~ 'upper_fault', 'on') or is_state(b ~ 'lower_fault', 'on') -%}
          {%- if is_state(b ~ 'power', 'off') and not fault -%}
            Off
          {%- elif readings -%}
            {{- readings | join(' | ') | truncate(255, true, '…') -}}
          {%- elif is_state(s ~ 'upper_state', 'standby') and is_state(s ~ 'lower_state', 'standby') -%}
            Standby · Remote {{ {'on': 'on', 'off': 'off'}.get(states(b ~ 'remote_control'), 'unknown') }}
          {%- else -%}
            No drum readings
          {%- endif -%}
        attributes:
          status: >-
            {% set p = 'hwd120_b1558u_' %}
            {% set b = 'binary_sensor.' ~ p %}
            {% set drum_states = [states('sensor.' ~ p ~ 'upper_state'), states('sensor.' ~ p ~ 'lower_state')] %}
            {% if is_state(b ~ 'upper_fault', 'on') or is_state(b ~ 'lower_fault', 'on') %}fault
            {% elif is_state(b ~ 'power', 'off') %}off
            {% elif 'running' in drum_states %}running
            {% elif 'paused' in drum_states %}paused
            {% elif 'delayed' in drum_states %}delayed
            {% elif 'completed' in drum_states %}completed
            {% elif drum_states == ['standby', 'standby'] %}standby
            {% else %}unknown{% endif %}
          power: >-
            {% set e = 'binary_sensor.hwd120_b1558u_power' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          remote_control: >-
            {% set e = 'binary_sensor.hwd120_b1558u_remote_control' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          local_lock: >-
            {% set e = 'binary_sensor.hwd120_b1558u_local_control_lock' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          child_lock: >-
            {% set e = 'switch.hwd120_b1558u_child_lock' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          buzzer: >-
            {% set e = 'switch.hwd120_b1558u_sound' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          upper: >-
            {% set p = 'hwd120_b1558u_upper_' %}
            {% set s = 'sensor.' ~ p %}
            {% set b = 'binary_sensor.' ~ p %}
            {% set n = namespace(data={}, options=[], labels=[]) %}
            {% for key, suffix in [('execution_state', 'state'), ('program', 'programme_now'), ('phase', 'phase'), ('fault', 'fault_code')] %}
              {% set e = s ~ suffix %}
              {% set n.data = dict(n.data, **{('fault_code' if key == 'fault' else key): states(e) if has_value(e) else none, (key ~ '_label'): state_translated(e) if has_value(e) else none}) %}
            {% endfor %}
            {% for key, suffix in [('remaining_minutes', 'time_left'), ('temperature_c', 'target_temp'), ('wash_stage_minutes', 'wash_stage_time'), ('rinses', 'rinse_count'), ('spin_stage_minutes', 'spin_stage_time')] %}
              {% set value = states(s ~ suffix) %}
              {% set n.data = dict(n.data, **{key: value | int if is_number(value) else none}) %}
            {% endfor %}
            {% for key, suffix, label in [('high_water', 'water_plus_on', 'Water Plus'), ('easy_iron', 'easy_ironing_on', 'Easy Ironing'), ('intensive', 'intensive_on', 'Intensive'), ('eco', 'eco_wash_on', 'Eco. wash'), ('soak', 'soak_wash_on', 'Soak wash'), ('night', 'night_wash_on', 'Night wash'), ('smart_mode', 'smart_wash_on', 'Smart washing'), ('delay_active', 'end_delay_active', 'End time delay')] if is_state(b ~ suffix, 'on') %}
              {% set n.options = n.options + [key] %}
              {% set n.labels = n.labels + [label] %}
            {% endfor %}
            {{ dict(n.data,
                estimated_end=(now() + timedelta(minutes=n.data.remaining_minutes)).strftime('%H:%M') if n.data.remaining_minutes is not none and n.data.execution_state in ['running', 'delayed'] else none,
                door_locked=is_state(b ~ 'door_lock', 'on') if has_value(b ~ 'door_lock') else none,
                fault=is_state(b ~ 'fault', 'on') if has_value(b ~ 'fault') else none,
                active_faults=state_attr(s ~ 'fault_code', 'active_faults'), options=n.options, options_labels=n.labels) }}
          lower: >-
            {% set p = 'hwd120_b1558u_lower_' %}
            {% set s = 'sensor.' ~ p %}
            {% set b = 'binary_sensor.' ~ p %}
            {% set n = namespace(data={}, options=[], labels=[]) %}
            {% for key, suffix in [('execution_state', 'state'), ('program', 'programme_now'), ('phase', 'phase'), ('fault', 'fault_code'), ('drying_mode', 'drying_mode')] %}
              {% set e = s ~ suffix %}
              {% set n.data = dict(n.data, **{('fault_code' if key == 'fault' else key): states(e) if has_value(e) else none, (key ~ '_label'): state_translated(e) if has_value(e) else none}) %}
            {% endfor %}
            {% for key, suffix in [('remaining_minutes', 'time_left'), ('temperature_c', 'target_temp'), ('wash_stage_minutes', 'wash_stage_time'), ('rinses', 'rinse_count'), ('spin_stage_minutes', 'spin_stage_time')] %}
              {% set value = states(s ~ suffix) %}
              {% set n.data = dict(n.data, **{key: value | int if is_number(value) else none}) %}
            {% endfor %}
            {% for key, suffix, label in [('high_water', 'water_plus_on', 'Water Plus'), ('easy_iron', 'easy_ironing_on', 'Easy Ironing'), ('intensive', 'intensive_on', 'Intensive'), ('eco', 'eco_wash_on', 'Eco. wash'), ('soak', 'soak_wash_on', 'Soak wash'), ('night', 'night_wash_on', 'Night wash'), ('smart_mode', 'smart_wash_on', 'Smart washing'), ('delay_active', 'end_delay_active', 'End time delay')] if is_state(b ~ suffix, 'on') %}
              {% set n.options = n.options + [key] %}
              {% set n.labels = n.labels + [label] %}
            {% endfor %}
            {{ dict(n.data,
                estimated_end=(now() + timedelta(minutes=n.data.remaining_minutes)).strftime('%H:%M') if n.data.remaining_minutes is not none and n.data.execution_state in ['running', 'delayed'] else none,
                door_locked=is_state(b ~ 'door_lock', 'on') if has_value(b ~ 'door_lock') else none,
                fault=is_state(b ~ 'fault', 'on') if has_value(b ~ 'fault') else none,
                active_faults=state_attr(s ~ 'fault_code', 'active_faults'), options=n.options, options_labels=n.labels) }}
```

## Russian template sensor

Используйте этот вариант, если в Home Assistant выбран русский язык сервера
(**Настройки → Система → Общие**). Суффиксы `entity_id` у сущностей интеграции
остаются английскими (`_power`, `_upper_state`, `_upper_programme_now` и т.д.)
— меняйте только префикс `hwd120_b1558u_`, если устройство добавлено с другим
именем (проверьте `entity_id` бинарного сенсора питания в карточке устройства).

```yaml
template:
  - sensor:
      - name: "Стиральная машина: сводка"
        unique_id: hwd120_b1558u_summary
        default_entity_id: sensor.hwd120_b1558u_summary
        availability: >-
          {{ has_value('binary_sensor.hwd120_b1558u_power') }}
        icon: >-
          {% set p = 'binary_sensor.hwd120_b1558u_' %}
          {% if is_state(p ~ 'upper_fault', 'on') or is_state(p ~ 'lower_fault', 'on') %}
            mdi:washing-machine-alert
          {% elif is_state(p ~ 'power', 'off') %}
            mdi:washing-machine-off
          {% else %}
            mdi:washing-machine
          {% endif %}
        state: >-
          {%- set p = 'hwd120_b1558u_' -%}
          {%- macro drum(d, label) -%}
            {%- set s = 'sensor.' ~ p ~ d ~ '_' -%}
            {%- set b = 'binary_sensor.' ~ p ~ d ~ '_' -%}
            {%- set st = states(s ~ 'state') -%}
            {%- set phase = states(s ~ 'phase') -%}
            {%- set parts = namespace(items=[]) -%}
            {%- if is_state(b ~ 'fault', 'on') -%}
              {%- set code = states(s ~ 'fault_code') -%}
              {%- set parts.items = ['⚠ ' ~ (state_translated(s ~ 'fault_code') if code not in ['none', 'unknown', 'unavailable'] else 'Ошибка')] -%}
            {%- endif -%}
            {%- if st in ['running', 'paused', 'delayed', 'completed'] and states(s ~ 'programme_now') not in ['none', 'unknown', 'unavailable'] -%}
              {%- set parts.items = parts.items + [state_translated(s ~ 'programme_now')] -%}
            {%- endif -%}
            {%- if st in ['paused', 'delayed', 'completed'] -%}
              {%- set label_st = {'paused': 'Пауза', 'delayed': 'Отложено', 'completed': 'Завершено'}[st] -%}
              {%- set parts.items = parts.items + [label_st ~ (' 🔒' if st == 'completed' and is_state(b ~ 'door_lock', 'on') else '')] -%}
            {%- elif st == 'running' -%}
              {%- set parts.items = parts.items + [state_translated(s ~ 'phase') if phase not in ['none', 'standby', 'unknown', 'unavailable'] else 'Работает'] -%}
            {%- endif -%}
            {%- if st in ['running', 'paused', 'delayed'] -%}
              {%- set temp = states(s ~ 'target_temp') -%}
              {%- set rem = states(s ~ 'time_left') -%}
              {%- if is_number(temp) -%}{%- set parts.items = parts.items + [(temp | int) ~ '°C'] -%}{%- endif -%}
              {%- if is_number(rem) -%}
                {%- set m = rem | int -%}
                {%- if m >= 60 -%}
                  {%- set parts.items = parts.items + [m // 60 ~ ' ч' ~ (' ' ~ (m % 60) ~ ' мин' if m % 60 else '')] -%}
                {%- else -%}
                  {%- set parts.items = parts.items + [m ~ ' мин'] -%}
                {%- endif -%}
              {%- endif -%}
            {%- endif -%}
            {{- label ~ ': ' ~ parts.items | join(' · ') if parts.items else '' -}}
          {%- endmacro -%}
          {%- set upper = drum('upper', 'Верхний') -%}
          {%- set lower = drum('lower', 'Нижний') -%}
          {%- set readings = [upper, lower] | reject('equalto', '') | list -%}
          {%- set b = 'binary_sensor.' ~ p -%}
          {%- set s = 'sensor.' ~ p -%}
          {%- set fault = is_state(b ~ 'upper_fault', 'on') or is_state(b ~ 'lower_fault', 'on') -%}
          {%- if is_state(b ~ 'power', 'off') and not fault -%}
            Выключена
          {%- elif readings -%}
            {{- readings | join(' | ') | truncate(255, true, '…') -}}
          {%- elif is_state(s ~ 'upper_state', 'standby') and is_state(s ~ 'lower_state', 'standby') -%}
            Ожидание · ДУ {{ {'on': 'вкл', 'off': 'выкл'}.get(states(b ~ 'remote_control'), 'неизвестно') }}
          {%- else -%}
            Нет показаний обоих барабанов
          {%- endif -%}
        attributes:
          status: >-
            {% set p = 'hwd120_b1558u_' %}
            {% set b = 'binary_sensor.' ~ p %}
            {% set drum_states = [states('sensor.' ~ p ~ 'upper_state'), states('sensor.' ~ p ~ 'lower_state')] %}
            {% if is_state(b ~ 'upper_fault', 'on') or is_state(b ~ 'lower_fault', 'on') %}fault
            {% elif is_state(b ~ 'power', 'off') %}off
            {% elif 'running' in drum_states %}running
            {% elif 'paused' in drum_states %}paused
            {% elif 'delayed' in drum_states %}delayed
            {% elif 'completed' in drum_states %}completed
            {% elif drum_states == ['standby', 'standby'] %}standby
            {% else %}unknown{% endif %}
          power: >-
            {% set e = 'binary_sensor.hwd120_b1558u_power' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          remote_control: >-
            {% set e = 'binary_sensor.hwd120_b1558u_remote_control' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          local_lock: >-
            {% set e = 'binary_sensor.hwd120_b1558u_local_control_lock' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          child_lock: >-
            {% set e = 'switch.hwd120_b1558u_child_lock' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          buzzer: >-
            {% set e = 'switch.hwd120_b1558u_sound' %}
            {{ is_state(e, 'on') if has_value(e) else none }}
          upper: >-
            {% set p = 'hwd120_b1558u_upper_' %}
            {% set s = 'sensor.' ~ p %}
            {% set b = 'binary_sensor.' ~ p %}
            {% set n = namespace(data={}, options=[], labels=[]) %}
            {% for key, suffix in [('execution_state', 'state'), ('program', 'programme_now'), ('phase', 'phase'), ('fault', 'fault_code')] %}
              {% set e = s ~ suffix %}
              {% set n.data = dict(n.data, **{('fault_code' if key == 'fault' else key): states(e) if has_value(e) else none, (key ~ '_label'): state_translated(e) if has_value(e) else none}) %}
            {% endfor %}
            {% for key, suffix in [('remaining_minutes', 'time_left'), ('temperature_c', 'target_temp'), ('wash_stage_minutes', 'wash_stage_time'), ('rinses', 'rinse_count'), ('spin_stage_minutes', 'spin_stage_time')] %}
              {% set value = states(s ~ suffix) %}
              {% set n.data = dict(n.data, **{key: value | int if is_number(value) else none}) %}
            {% endfor %}
            {% for key, suffix, label in [('high_water', 'water_plus_on', 'Больше воды'), ('easy_iron', 'easy_ironing_on', 'Легкая глажка'), ('intensive', 'intensive_on', 'Интенсивная'), ('eco', 'eco_wash_on', 'Эко-стирка'), ('soak', 'soak_wash_on', 'Замачивание'), ('night', 'night_wash_on', 'Ночная стирка'), ('smart_mode', 'smart_wash_on', 'Умная стирка'), ('delay_active', 'end_delay_active', 'Задержка времени окончания')] if is_state(b ~ suffix, 'on') %}
              {% set n.options = n.options + [key] %}
              {% set n.labels = n.labels + [label] %}
            {% endfor %}
            {{ dict(n.data,
                estimated_end=(now() + timedelta(minutes=n.data.remaining_minutes)).strftime('%H:%M') if n.data.remaining_minutes is not none and n.data.execution_state in ['running', 'delayed'] else none,
                door_locked=is_state(b ~ 'door_lock', 'on') if has_value(b ~ 'door_lock') else none,
                fault=is_state(b ~ 'fault', 'on') if has_value(b ~ 'fault') else none,
                active_faults=state_attr(s ~ 'fault_code', 'active_faults'), options=n.options, options_labels=n.labels) }}
          lower: >-
            {% set p = 'hwd120_b1558u_lower_' %}
            {% set s = 'sensor.' ~ p %}
            {% set b = 'binary_sensor.' ~ p %}
            {% set n = namespace(data={}, options=[], labels=[]) %}
            {% for key, suffix in [('execution_state', 'state'), ('program', 'programme_now'), ('phase', 'phase'), ('fault', 'fault_code'), ('drying_mode', 'drying_mode')] %}
              {% set e = s ~ suffix %}
              {% set n.data = dict(n.data, **{('fault_code' if key == 'fault' else key): states(e) if has_value(e) else none, (key ~ '_label'): state_translated(e) if has_value(e) else none}) %}
            {% endfor %}
            {% for key, suffix in [('remaining_minutes', 'time_left'), ('temperature_c', 'target_temp'), ('wash_stage_minutes', 'wash_stage_time'), ('rinses', 'rinse_count'), ('spin_stage_minutes', 'spin_stage_time')] %}
              {% set value = states(s ~ suffix) %}
              {% set n.data = dict(n.data, **{key: value | int if is_number(value) else none}) %}
            {% endfor %}
            {% for key, suffix, label in [('high_water', 'water_plus_on', 'Больше воды'), ('easy_iron', 'easy_ironing_on', 'Легкая глажка'), ('intensive', 'intensive_on', 'Интенсивная'), ('eco', 'eco_wash_on', 'Эко-стирка'), ('soak', 'soak_wash_on', 'Замачивание'), ('night', 'night_wash_on', 'Ночная стирка'), ('smart_mode', 'smart_wash_on', 'Умная стирка'), ('delay_active', 'end_delay_active', 'Задержка времени окончания')] if is_state(b ~ suffix, 'on') %}
              {% set n.options = n.options + [key] %}
              {% set n.labels = n.labels + [label] %}
            {% endfor %}
            {{ dict(n.data,
                estimated_end=(now() + timedelta(minutes=n.data.remaining_minutes)).strftime('%H:%M') if n.data.remaining_minutes is not none and n.data.execution_state in ['running', 'delayed'] else none,
                door_locked=is_state(b ~ 'door_lock', 'on') if has_value(b ~ 'door_lock') else none,
                fault=is_state(b ~ 'fault', 'on') if has_value(b ~ 'fault') else none,
                active_faults=state_attr(s ~ 'fault_code', 'active_faults'), options=n.options, options_labels=n.labels) }}
```

## Optional dashboard Markdown card

Display the summary state together with expanded drum details on any Lovelace
dashboard using a native Markdown card:

### English card

```yaml
type: markdown
title: Washer summary
content: >-
  {% set e = 'sensor.hwd120_b1558u_summary' %}
  {% set st = state_attr(e, 'status') %}
  **Status:** {{ states(e) }}
  {% if st not in ['off', 'unavailable', 'unknown'] %}
  Remote control: {{ 'On' if state_attr(e, 'remote_control') else 'Off' }} · Local lock: {{ 'On' if state_attr(e, 'local_lock') else 'Off' }}
  {% for title, key in [('Upper drum', 'upper'), ('Lower drum', 'lower')] %}
  {% set d = state_attr(e, key) or {} %}
  {% if d.get('execution_state') not in ['standby', none] or d.get('fault') %}

  **{{ title }}:** {{ d.get('program_label') or '—' }} — {{ d.get('phase_label') or d.get('execution_state_label') }}
  {%- if d.get('temperature_c') is not none %} · {{ d.get('temperature_c') }}°C{% endif %}
  {%- if d.get('remaining_minutes') is not none %} · {{ d.get('remaining_minutes') }} min{% endif %}
  {%- if d.get('estimated_end') %} (~{{ d.get('estimated_end') }}){% endif %}
  {%- if d.get('drying_mode') and d.get('drying_mode') != 'off' %} · Drying: {{ d.get('drying_mode_label') }}{% endif %}
  {%- if d.get('options_labels') %}{{ '\n' }}Options: {{ d.get('options_labels') | join(', ') }}{% endif %}
  {%- if d.get('fault') %}{{ '\n' }}⚠ Fault: {{ d.get('fault_label') }}{% endif %}
  {% endif %}
  {% endfor %}
  {% endif %}
```

### Russian card

```yaml
type: markdown
title: Стиральная машина
content: >-
  {% set e = 'sensor.hwd120_b1558u_summary' %}
  {% set st = state_attr(e, 'status') %}
  **Статус:** {{ states(e) }}
  {% if st not in ['off', 'unavailable', 'unknown'] %}
  ДУ: {{ 'вкл' if state_attr(e, 'remote_control') else 'выкл' }} · Местн. блокировка: {{ 'вкл' if state_attr(e, 'local_lock') else 'выкл' }}
  {% for title, key in [('Верхний барабан', 'upper'), ('Нижний барабан', 'lower')] %}
  {% set d = state_attr(e, key) or {} %}
  {% if d.get('execution_state') not in ['standby', none] or d.get('fault') %}

  **{{ title }}:** {{ d.get('program_label') or '—' }} — {{ d.get('phase_label') or d.get('execution_state_label') }}
  {%- if d.get('temperature_c') is not none %} · {{ d.get('temperature_c') }}°C{% endif %}
  {%- if d.get('remaining_minutes') is not none %} · {{ d.get('remaining_minutes') }} мин{% endif %}
  {%- if d.get('estimated_end') %} (~{{ d.get('estimated_end') }}){% endif %}
  {%- if d.get('drying_mode') and d.get('drying_mode') != 'off' %} · Сушка: {{ d.get('drying_mode_label') }}{% endif %}
  {%- if d.get('options_labels') %}{{ '\n' }}Опции: {{ d.get('options_labels') | join(', ') }}{% endif %}
  {%- if d.get('fault') %}{{ '\n' }}⚠ Ошибка: {{ d.get('fault_label') }}{% endif %}
  {% endif %}
  {% endfor %}
  {% endif %}
```

## Optional Recorder exclusion

Because `estimated_end` recomputes on clock/remaining-time updates and the
underlying integration entities already record authoritative history, excluding
the summary sensor avoids unnecessary Recorder database writes:

```yaml
recorder:
  exclude:
    entities:
      - sensor.hwd120_b1558u_summary
```

Licensed under [CC BY 4.0](LICENSE). Attribution: U+ Reborn contributors,
[U+ Reborn repository](https://github.com/Zuz666/uplus-reborn).
