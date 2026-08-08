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

function apiHeaders_() {
  var cfg = getConfig_();
  var headers = { 'X-Addon-Secret': cfg.secret };
  var identityToken = ScriptApp.getIdentityToken();
  if (identityToken) headers['X-Addon-Identity'] = identityToken;
  return headers;
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
      headers: apiHeaders_(),
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
      headers: apiHeaders_(),
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

function onGmailMessageOpen(e) {
  var messageId = e && e.gmail ? e.gmail.messageId : null;
  return buildMessageCard_(messageId);
}

function buildMessageCard_(messageId) {
  var section = CardService.newCardSection();
  if (!messageId) {
    section.addWidget(CardService.newTextParagraph().setText('Open an email first.'));
    return CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Email Triage Assistant'))
      .addSection(section)
      .build();
  }

  var email = getUserEmail_();
  var context = apiGet_(
    '/api/addon/message-context?email=' +
      encodeURIComponent(email) +
      '&gmail_id=' +
      encodeURIComponent(messageId)
  );
  if (!context || context.error) {
    section.addWidget(
      CardService.newTextParagraph().setText(
        escapeHtml_(context && context.error ? context.error : 'Could not load this email.')
      )
    );
    if (context && context.status === 404) {
      section.addWidget(
        CardService.newTextButton()
          .setText('Connect Gmail')
          .setOpenLink(connectGmailLink_())
      );
    }
    return CardService.newCardBuilder()
      .setHeader(CardService.newCardHeader().setTitle('Email Triage Assistant'))
      .addSection(section)
      .build();
  }

  section.addWidget(
    CardService.newDecoratedText()
      .setTopLabel('Current category')
      .setText(escapeHtml_(context.current_category || 'Not categorized yet'))
  );
  var categoryInput = CardService.newSelectionInput()
    .setType(CardService.SelectionInputType.DROPDOWN)
    .setFieldName('category')
    .setTitle('Move to');
  var categories = context.categories || [];
  for (var i = 0; i < categories.length; i++) {
    var category = categories[i];
    categoryInput.addItem(
      category,
      category,
      category === context.current_category || (!context.current_category && i === 0)
    );
  }
  section.addWidget(categoryInput);
  section.addWidget(
    CardService.newTextButton()
      .setText('Save & Teach Assistant')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('saveCategoryCorrection')
          .setParameters({ gmailId: messageId })
      )
  );
  section.addWidget(
    CardService.newTextButton()
      .setText('Summarize this email')
      .setOnClickAction(
        CardService.newAction()
          .setFunctionName('summarizeCurrentMessage')
          .setParameters({ gmailId: messageId })
      )
  );
  return CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Email Triage Assistant'))
    .addSection(section)
    .build();
}

function summarizeCurrentMessage(e) {
  var messageId =
    (e && e.parameters && e.parameters.gmailId) ||
    (e && e.gmail ? e.gmail.messageId : null);
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

function saveCategoryCorrection(e) {
  var messageId = e && e.parameters ? e.parameters.gmailId : null;
  var category = formValue_(e, 'category', '');
  if (!messageId || !category) {
    return notify_('Choose a category first.');
  }
  var result = apiPost_('/api/addon/feedback', {
    email: getUserEmail_(),
    gmail_id: messageId,
    category: category,
  });
  if (!result || result.error) {
    return notify_(result && result.error ? result.error : 'Could not save this correction.');
  }
  return CardService.newActionResponseBuilder()
    .setNotification(
      CardService.newNotification().setText(
        'Moved to ' + result.new_category + '. Similar emails will use this correction.'
      )
    )
    .setNavigation(
      CardService.newNavigation().updateCard(buildMessageCard_(messageId))
    )
    .build();
}

function notify_(text) {
  return CardService.newActionResponseBuilder()
    .setNotification(CardService.newNotification().setText(text))
    .build();
}

function connectGmailLink_() {
  var cfg = getConfig_();
  return CardService.newOpenLink()
    .setUrl(cfg.backendUrl + '/auth/login?return_to=addon')
    .setOpenAs(CardService.OpenAs.FULL_SIZE)
    .setOnClose(CardService.OnClose.RELOAD_ADD_ON);
}

function openAttentionMails(e) {
  var email = getUserEmail_();
  var digest = apiGet_('/api/addon/digest?email=' + encodeURIComponent(email));
  var items = digest && !digest.error ? digest.alerts || [] : [];
  var section = CardService.newCardSection();
  if (!items.length) {
    section.addWidget(
      CardService.newTextParagraph().setText('No unread important mail right now.')
    );
  }
  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var widget = CardService.newDecoratedText()
      .setTopLabel(escapeHtml_(item.category || 'Important'))
      .setText(escapeHtml_(item.subject || '(no subject)'))
      .setBottomLabel(escapeHtml_(item.sender || ''))
      .setWrapText(true);
    if (item.gmail_url) {
      widget.setOpenLink(CardService.newOpenLink().setUrl(item.gmail_url));
    }
    section.addWidget(widget);
  }
  var card = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Needs your attention (' + items.length + ')'))
    .addSection(section)
    .build();
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().pushCard(card))
    .build();
}

function openUpcomingDeadlines(e) {
  var email = getUserEmail_();
  var digest = apiGet_('/api/addon/digest?email=' + encodeURIComponent(email));
  var items = digest && !digest.error ? digest.deadlines || [] : [];
  var section = CardService.newCardSection();
  if (!items.length) {
    section.addWidget(
      CardService.newTextParagraph().setText('No upcoming deadlines right now.')
    );
  }
  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var widget = CardService.newDecoratedText()
      .setTopLabel('Due ' + escapeHtml_(item.due_date || ''))
      .setText(escapeHtml_(item.subject || item.description || 'Deadline'))
      .setBottomLabel(escapeHtml_(item.sender || ''))
      .setWrapText(true);
    if (item.gmail_url) {
      widget.setOpenLink(CardService.newOpenLink().setUrl(item.gmail_url));
    }
    section.addWidget(widget);
  }
  var card = CardService.newCardBuilder()
    .setHeader(CardService.newCardHeader().setTitle('Upcoming deadlines (' + items.length + ')'))
    .addSection(section)
    .build();
  return CardService.newActionResponseBuilder()
    .setNavigation(CardService.newNavigation().pushCard(card))
    .build();
}

function buildHomeCard_() {
  var email = getUserEmail_();
  var status = apiGet_('/api/addon/status?email=' + encodeURIComponent(email));
  var section = CardService.newCardSection();

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
        'Connect this Gmail account once to start triage.'
      )
    );
    section.addWidget(
      CardService.newTextButton()
        .setText('Connect Gmail')
        .setOpenLink(connectGmailLink_())
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
        parts.push(escapeHtml_(key) + ': ' + Number(counts[key] || 0));
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

    var alerts = digest.alerts || [];
    var deadlines = digest.deadlines || [];
    if (alerts.length || deadlines.length) {
      var quickLinks = CardService.newButtonSet();
      if (alerts.length) {
        quickLinks.addButton(
          CardService.newTextButton()
            .setText('Needs your attention (' + alerts.length + ')')
            .setOnClickAction(
              CardService.newAction().setFunctionName('openAttentionMails')
            )
        );
      }
      if (deadlines.length) {
        quickLinks.addButton(
          CardService.newTextButton()
            .setText('Upcoming deadlines (' + deadlines.length + ')')
            .setOnClickAction(
              CardService.newAction().setFunctionName('openUpcomingDeadlines')
            )
        );
      }
      section.addWidget(quickLinks);
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
  builder.addSection(section);
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
