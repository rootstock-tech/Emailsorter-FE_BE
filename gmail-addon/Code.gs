/**
 * Email Triage Assistant — Gmail Add-on (Apps Script).
 *
 * A thin UI that lives inside Gmail (web + mobile) and talks to the Python
 * backend. The backend does the AI sorting, learned rules, auto-sort, etc.;
 * this card only shows status and lets the user run triage or set the auto-sort
 * interval.
 *
 * Before deploying, set two Script Properties (Project Settings -> Script
 * properties), and make sure the backend is reachable at a public HTTPS URL:
 *   BACKEND_URL   e.g. https://your-host.example.com   (no trailing slash needed)
 *   ADDON_SECRET  must equal ADDON_SHARED_SECRET in the backend .env
 *
 * The user must have connected their Gmail once via the web dashboard
 * ("Connect Gmail") so the backend has a stored token for them.
 */

var AUTO_INTERVALS = ['5', '10', '15', '30', '60'];

function getConfig_() {
  var props = PropertiesService.getScriptProperties();
  return {
    backendUrl: (props.getProperty('BACKEND_URL') || '').replace(/\/+$/, ''),
    secret: props.getProperty('ADDON_SECRET') || '',
  };
}

function getUserEmail_() {
  return Session.getActiveUser().getEmail() || Session.getEffectiveUser().getEmail();
}

function escapeHtml_(text) {
  return String(text || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function formValue_(e, fieldName, fallback) {
  var inputs = e && e.commonEventObject && e.commonEventObject.formInputs;
  var values = inputs && inputs[fieldName] && inputs[fieldName].stringInputs;
  if (values && values.value && values.value.length) return values.value[0];
  if (e && e.formInput && e.formInput[fieldName]) return e.formInput[fieldName];
  return fallback;
}

function parseJson_(res) {
  var status = res.getResponseCode();
  try {
    var parsed = JSON.parse(res.getContentText());
    if (status >= 400) {
      return { error: parsed.error || 'Backend returned HTTP ' + status, status: status };
    }
    return parsed;
  } catch (err) {
    return { error: 'Bad response (' + res.getResponseCode() + ')' };
  }
}

function apiGet_(path) {
  var cfg = getConfig_();
  if (!cfg.backendUrl || !cfg.secret) {
    return { error: 'BACKEND_URL or ADDON_SECRET is not configured.' };
  }
  try {
    var res = UrlFetchApp.fetch(cfg.backendUrl + path, {
      method: 'get',
      muteHttpExceptions: true,
      headers: { 'X-Addon-Secret': cfg.secret },
    });
    return parseJson_(res);
  } catch (err) {
    return { error: 'Backend is unreachable: ' + err.message };
  }
}

function apiPost_(path, payload) {
  var cfg = getConfig_();
  if (!cfg.backendUrl || !cfg.secret) {
    return { error: 'BACKEND_URL or ADDON_SECRET is not configured.' };
  }
  try {
    var res = UrlFetchApp.fetch(cfg.backendUrl + path, {
      method: 'post',
      contentType: 'application/json',
      muteHttpExceptions: true,
      headers: { 'X-Addon-Secret': cfg.secret },
      payload: JSON.stringify(payload),
    });
    return parseJson_(res);
  } catch (err) {
    return { error: 'Backend is unreachable: ' + err.message };
  }
}

function onHomepage(e) {
  return buildHomeCard_();
}

// Contextual card shown when a message is open: a single "Summarize" button.
function onGmailMessageOpen(e) {
  var section = CardService.newCardSection();
  section.addWidget(
    CardService.newTextParagraph().setText('Get a quick summary of this email.')
  );
  section.addWidget(
    CardService.newTextButton()
      .setText('Summarize this email')
      .setOnClickAction(CardService.newAction().setFunctionName('summarizeCurrentMessage'))
  );
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Summarize'))
    .addSection(section)
    .build();
}

function summarizeCurrentMessage(e) {
  var messageId = e && e.gmail ? e.gmail.messageId : null;
  if (!messageId) {
    return notify_('Open an email first, then tap Summarize.');
  }
  var result = apiPost_('/api/addon/summarize', {
    email: getUserEmail_(),
    gmail_id: messageId,
  });
  if (!result || result.error || !result.summary) {
    return notify_(result && result.error ? result.error : 'Could not summarize right now. Try again.');
  }

  var section = CardService.newCardSection();
  section.addWidget(
    CardService.newTextParagraph().setText(
      escapeHtml_(result.summary).replace(/\n/g, '<br>')
    )
  );
  var card = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Summary'))
    .addSection(section)
    .build();
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().pushCard(card))
    .build();
}

function notify_(text) {
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(text))
    .build();
}

function buildHomeCard_() {
  var email = getUserEmail_();
  var status = apiGet_('/api/addon/status?email=' + encodeURIComponent(email));
  var section = CardService.newCardSection();
  var alertsSection = null;
  var deadlinesSection = null;

  if (!status || status.error) {
    section.addWidget(
      CardService.newTextParagraph().setText(
        'Setup or backend error: ' + escapeHtml_(status && status.error ? status.error : 'Unknown error')
      )
    );
    return CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Email Triage Assistant'))
      .addSection(section)
      .build();
  }

  if (status.connected === false) {
    section.addWidget(
      CardService.newTextParagraph().setText(
        'This account is not connected yet. Open the web dashboard once, click ' +
          '"Connect Gmail", then reopen this add-on.'
      )
    );
    return CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Email Triage Assistant'))
      .addSection(section)
      .build();
  }

  var statusText =
    status.status === 'running'
      ? 'Sorting in progress (' + (status.percent || 0) + '%)'
      : 'Ready';
  section.addWidget(
    CardService.newDecoratedText().setTopLabel('Status').setText(statusText)
  );

  var running = status.status === 'running';
  if (running) {
    // Locked while a run is active; refresh to pull the latest progress/results.
    section.addWidget(
      CardService.newTextButton()
        .setText('Refresh')
        .setOnClickAction(CardService.newAction().setFunctionName('refreshCard'))
    );
  } else {
    section.addWidget(
      CardService.newDecoratedText().setTopLabel('Sort mail from').setText('How far back')
    );
    section.addWidget(
      CardService.newSelectionInput()
        .setType(CardService.SelectionInputType.DROPDOWN)
        .setFieldName('range')
        .addItem('Up to 1 day', '1d', true)
        .addItem('Up to 1 week', '1w', false)
    );
    section.addWidget(
      CardService.newTextButton()
        .setText('Run triage now')
        .setOnClickAction(CardService.newAction().setFunctionName('runTriage'))
    );
  }

  // Digest, alerts, deadlines, and undo, pulled in one call.
  var digest = apiGet_('/api/addon/digest?email=' + encodeURIComponent(email));
  if (digest && !digest.error) {
    var counts = digest.counts;
    if (counts) {
      var total = 0;
      var parts = [];
      for (var key in counts) {
        if (key === 'FAQ (drafted)') continue;
        total += Number(counts[key]) || 0;
        parts.push(key + ': ' + counts[key]);
      }
      section.addWidget(
        CardService.newDecoratedText()
          .setTopLabel('Last sort')
          .setText(total + ' emails sorted')
      );
      if (parts.length) {
        section.addWidget(
          CardService.newTextParagraph().setText(parts.join('  \u00b7  '))
        );
      }
    }
    section.addWidget(
      CardService.newDecoratedText()
        .setTopLabel('Learned rules')
        .setText(
          (digest.learned_active || 0) +
            ' of ' +
            (digest.learned_total || 0) +
            ' active guidance signals'
        )
    );

    // Undo the last sort, when there is something to revert.
    if (digest.undo_count && digest.undo_count > 0) {
      section.addWidget(
        CardService.newTextButton()
          .setText('Undo last sort (' + digest.undo_count + ')')
          .setOnClickAction(CardService.newAction().setFunctionName('undoRun'))
      );
    }

    // Alerts: unread red-flag / needs-action mail pinned at the very top.
    var alerts = digest.alerts || [];
    if (alerts.length) {
      alertsSection = CardService.newCardSection().setHeader('\u26a0 Needs your attention');
      alertsSection.addWidget(
        CardService.newTextParagraph().setText(
          "You haven't handled these " + alerts.length + ' important mail yet.'
        )
      );
      for (var a = 0; a < alerts.length; a++) {
        var al = alerts[a];
        var aw = CardService.newDecoratedText()
          .setTopLabel(al.category || 'Important')
          .setText(al.subject || '(no subject)')
          .setBottomLabel(al.sender || '')
          .setWrapText(true);
        if (al.gmail_url) {
          aw.setOpenLink(CardService.newOpenLink().setUrl(al.gmail_url));
        }
        alertsSection.addWidget(aw);
      }
    }

    // Deadlines: upcoming due dates pulled from mail.
    var deadlines = digest.deadlines || [];
    if (deadlines.length) {
      deadlinesSection = CardService.newCardSection().setHeader('\u23f0 Upcoming deadlines');
      for (var d = 0; d < deadlines.length; d++) {
        var dl = deadlines[d];
        var dw = CardService.newDecoratedText()
          .setTopLabel('Due ' + (dl.due_date || ''))
          .setText(dl.subject || dl.description || 'Deadline')
          .setBottomLabel(dl.sender || '')
          .setWrapText(true);
        if (dl.gmail_url) {
          dw.setOpenLink(CardService.newOpenLink().setUrl(dl.gmail_url));
        }
        deadlinesSection.addWidget(dw);
      }
    }
  }

  section.addWidget(
    CardService.newDecoratedText().setTopLabel('Auto-sort').setText('How often to sort')
  );
  var dropdown = CardService.newSelectionInput()
    .setType(CardService.SelectionInputType.DROPDOWN)
    .setFieldName('interval')
    .addItem('Off', 'off', !status.interval_minutes);
  for (var i = 0; i < AUTO_INTERVALS.length; i++) {
    var v = AUTO_INTERVALS[i];
    dropdown.addItem('Every ' + v + ' minutes', v, String(status.interval_minutes) === v);
  }
  dropdown.setOnChangeAction(CardService.newAction().setFunctionName('setAuto'));
  section.addWidget(dropdown);

  section.addWidget(
    CardService.newTextParagraph().setText(
      'Sorting runs automatically in the background; labels appear on your mail ' +
        'in Gmail. Use this only to trigger a run or change the schedule.'
    )
  );

  var builder = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Email Triage Assistant'));
  // Alerts pinned above everything else so they are seen first.
  if (alertsSection) {
    builder.addSection(alertsSection);
  }
  builder.addSection(section);
  if (deadlinesSection) {
    builder.addSection(deadlinesSection);
  }
  return builder.build();
}

function runTriage(e) {
  var email = getUserEmail_();
  var range = formValue_(e, 'range', '1d');
  var res = apiPost_('/api/addon/triage', { email: email, range: range });
  var msg =
    res && res.status === 'running'
      ? 'A run is already in progress.'
      : res && res.status === 'started'
      ? 'Triage started.'
      : 'Could not start triage.';
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(msg))
    .setNavigation(CardService.newNavigation().updateCard(buildHomeCard_()))
    .build();
}

function refreshCard(e) {
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().updateCard(buildHomeCard_()))
    .build();
}

function setAuto(e) {
  var email = getUserEmail_();
  var interval = formValue_(e, 'interval', '');
  var on = interval && interval !== 'off';
  var res = apiPost_('/api/addon/auto', {
    email: email,
    interval_minutes: on ? parseInt(interval, 10) : null,
  });
  var msg =
    res && res.error
      ? 'Could not update auto-sort: ' + res.error
      : res && res.interval_minutes
      ? 'Auto-sort on: every ' + res.interval_minutes + ' min'
      : 'Auto-sort off';
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(msg))
    .setNavigation(CardService.newNavigation().updateCard(buildHomeCard_()))
    .build();
}

function undoRun(e) {
  var email = getUserEmail_();
  var res = apiPost_('/api/addon/undo', { email: email });
  var msg =
    res && res.error
      ? 'Could not undo: ' + res.error
      : res && res.status === 'undone'
      ? 'Reverted ' + (res.count || 0) + ' emails.'
      : 'Nothing to undo.';
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(msg))
    .setNavigation(CardService.newNavigation().updateCard(buildHomeCard_()))
    .build();
}
