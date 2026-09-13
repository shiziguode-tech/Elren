"""Native constraint validation retains its behavior in either UI language."""

import pytest
from test_frontend_high_priority import APP_JS, _function_source, _run_javascript


@pytest.mark.parametrize(
    ("flag", "extra", "expected"),
    [
        ("valueMissing", "", "Please fill out this field."),
        ("valueMissing", "type:'checkbox',", "Please check this box."),
        ("valueMissing", "tagName:'SELECT',", "Please select an option."),
        ("badInput", "", "Please enter a valid value."),
        ("typeMismatch", "type:'email',", "Please enter a valid email address."),
        ("typeMismatch", "type:'url',", "Please enter a valid URL."),
        ("rangeUnderflow", "min:'1',", "Please enter a value greater than or equal to 1."),
        ("rangeOverflow", "max:'60',", "Please enter a value less than or equal to 60."),
        ("tooShort", "minLength:3,", "Please enter at least 3 characters."),
        ("tooLong", "maxLength:10,", "Please enter no more than 10 characters."),
        ("stepMismatch", "", "Please enter a value matching the allowed step."),
        ("patternMismatch", "", "Please match the requested format."),
    ],
)
def test_english_constraints_cover_native_hover_and_submit_errors(flag, extra, expected):
    result = _run_javascript(
        "(() => { globalThis.uiText = (zh,en) => en; "
        f"return localizedConstraintMessage({{{extra}validity:{{{flag}:true}}}}); }})()",
        "localizedConstraintMessage",
    )
    assert result == expected


def test_constraint_sync_clears_its_stale_error_and_preserves_other_errors():
    result = _run_javascript(
        """(() => {
          globalThis.uiText = (zh,en) => en;
          globalThis.localizedConstraintMessages = new WeakMap();
          const field = {
            willValidate:true, validationMessage:'', validity:{valueMissing:true,customError:false},
            setCustomValidity(message) {
              this.validationMessage=message; this.validity.customError=Boolean(message);
            },
          };
          syncLocalizedConstraint(field);
          const initial = field.validationMessage;
          field.validity.valueMissing=false;
          syncLocalizedConstraint(field);
          const cleared = field.validationMessage;
          field.setCustomValidity('Domain-specific error');
          syncLocalizedConstraint(field);
          const preserved = field.validationMessage;
          field.setCustomValidity(''); field.validity.valueMissing=true;
          syncLocalizedConstraint(field);
          field.willValidate=false;
          syncLocalizedConstraint(field);
          const disabled = field.validationMessage;
          globalThis.uiText = (zh,en) => zh;
          field.willValidate=true;
          syncLocalizedConstraint(field);
          return {initial,cleared,preserved,disabled,chinese:field.validationMessage};
        })()""",
        "localizedConstraintMessage", "syncLocalizedConstraint",
    )
    assert result == {
        "initial": "Please fill out this field.", "cleared": "",
        "preserved": "Domain-specific error", "disabled": "", "chinese": "请填写此字段。",
    }


def test_validation_delegation_runs_before_native_default_actions():
    source = APP_JS.read_text("utf-8")
    function = _function_source(source, "initializeLocalizedConstraints")
    for name in ('"input"', '"change"', '"pointerover"', '"focusin"', '"invalid"'):
        assert name in function
    assert 'event.key === "Enter"' in function
    assert 'refreshLocalizedConstraints(control.form)' in function
    assert 'queueMicrotask(() => refreshLocalizedConstraints(event.target))' in function
    assert 'event.preventDefault' not in function
    assert 'initializeLocalizedConstraints();' in source


def test_datetime_is_editable_local_text_with_an_offline_calendar():
    source = APP_JS.read_text("utf-8")
    function = _function_source(source, "initializeScheduleDatePickers")
    assert 'new window.AirDatepicker(input' in function
    assert 'window.ElrenDateLocales[isEnglish() ? "en" : "zh"]' in function
    assert 'keyboardNav: true' in function
    assert 'input.addEventListener("input"' in function
    assert 'input.addEventListener("focus"' in function
    assert 'localized-datetime-empty' not in source


def test_partially_entered_date_is_not_disguised_as_an_empty_input():
    result = _run_javascript(
        """(() => {
          const results=[];
          for (const value of ['', '2026-', '2026-09-06T09:00']) {
            results.push(Boolean(parseScheduleDateTime(value)));
          }
          return results;
        })()""",
        "parseScheduleDateTime",
    )
    assert result == [False, False, True]


def test_hidden_schedule_constraints_are_disabled_and_restored_by_type():
    result = _run_javascript(
        """(() => {
          const fields={};
          for (const id of ['scheduleKind','scheduleIntervalField','scheduleEndField',
              'scheduleIntervalValue','scheduleIntervalUnit','scheduleEndAt']) {
            fields['#'+id]={value:'',disabled:false,classList:{toggle(){}}};
          }
          globalThis.$=(id)=>fields[id];
          globalThis.isEnglish=()=>true;
          globalThis.syncSettingsSelectWidget=()=>{};
          globalThis.syncLocalizedConstraint=()=>{};
          globalThis.syncEnglishDateTimePlaceholder=()=>{};
          const results=[];
          for (const kind of ['interval','daily','at','interval']) {
            fields['#scheduleKind'].value=kind;
            updateScheduleFields();
            results.push(['scheduleIntervalValue','scheduleIntervalUnit','scheduleEndAt']
              .map(id=>fields['#'+id].disabled));
          }
          return results;
        })()""",
        "updateScheduleFields",
    )
    assert result == [[False, False, False], [True, True, False], [True, True, True], [False, False, False]]
