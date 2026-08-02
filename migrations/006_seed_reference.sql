INSERT INTO entity_types (code, name, description) VALUES
  ('CARD',    'Payment card',       'Salted hash of the card number. Recognises repeat use without storing the PAN.'),
  ('EMAIL',   'Email address',      'Normalised and hashed. Gmail dots and plus-tags are stripped before hashing.'),
  ('DEVICE',  'Device fingerprint', 'Client-side fingerprint. Strongest signal for linking accounts operated by one person.'),
  ('IP',      'IP address',         'Weakest identifier: shared by households, offices, carriers and VPNs.'),
  ('ACCOUNT', 'Customer account',   'The merchant-side account identifier.'),
  ('PHONE',   'Phone number',       'Normalised to E.164 before hashing.');

INSERT INTO decision_types (code, name, description, is_approval, sort_order) VALUES
  ('APPROVE',   'Approve',   'Let the payment through with no friction.', true, 10),
  ('CHALLENGE', 'Challenge', 'Allow, but require step-up authentication first. Shifts fraud liability to the issuer when 3DS succeeds.', true, 20),
  ('REVIEW',    'Review',    'Hold for a human. Expensive and slow, so reserved for high-value uncertainty.', false, 30),
  ('DECLINE',   'Decline',   'Refuse the payment.', false, 40);

-- The feature registry. Rules may only reference codes that exist here, so
-- a typo fails when the rule is written rather than never matching in
-- production and being mistaken for a quiet rule.
INSERT INTO feature_definitions (code, name, description, value_type, source) VALUES
  -- Straight from the transaction
  ('amount_minor',            'Amount (minor units)',     'Transaction value in the smallest currency unit.', 'NUMBER',  'TRANSACTION'),
  ('is_card_present',         'Card present',             'False for all e-commerce. Card-absent carries merchant liability.', 'BOOLEAN', 'TRANSACTION'),
  ('avs_match',               'AVS match level',          'FULL, PARTIAL, NONE or UNAVAILABLE.', 'STRING', 'TRANSACTION'),
  ('cvv_match',               'CVV match',                'Proves the card was in hand at purchase, not merely known.', 'BOOLEAN', 'TRANSACTION'),
  ('three_ds_status',         '3-D Secure status',        'AUTHENTICATED shifts fraud liability to the issuer.', 'STRING', 'TRANSACTION'),
  ('is_prepaid_card',         'Prepaid card',             'Prepaid cards can be bought with cash and carry no identity.', 'BOOLEAN', 'TRANSACTION'),

  -- Geography
  ('ip_billing_country_match','IP matches billing country','A mismatch is common (travel, VPN) so it is a weak signal alone.', 'BOOLEAN', 'DERIVED'),
  ('bin_billing_country_match','BIN matches billing country','Card issued in a different country from the billing address.', 'BOOLEAN', 'DERIVED'),
  ('shipping_billing_match',  'Shipping matches billing', 'A mismatch is normal for gifts, and normal for reshipping fraud.', 'BOOLEAN', 'DERIVED'),

  -- Velocity: the strongest family of signals
  ('velocity_card_1h',        'Card uses, 1 hour',        'Card testing looks like a burst of small charges.', 'NUMBER', 'VELOCITY'),
  ('velocity_card_24h',       'Card uses, 24 hours',      'Sustained use of one card across a day.', 'NUMBER', 'VELOCITY'),
  ('velocity_email_24h',      'Email uses, 24 hours',     'One email driving many payments.', 'NUMBER', 'VELOCITY'),
  ('velocity_device_1h',      'Device uses, 1 hour',      'One device driving many payments in a burst.', 'NUMBER', 'VELOCITY'),
  ('velocity_ip_1h',          'IP uses, 1 hour',          'Weak on its own: offices and carriers share addresses.', 'NUMBER', 'VELOCITY'),
  ('velocity_amount_card_24h','Card amount, 24 hours',    'Total value pushed through one card in a day.', 'NUMBER', 'VELOCITY'),
  ('declines_card_24h',       'Declines on card, 24h',    'Repeated declines then an approval is the classic card-testing shape.', 'NUMBER', 'VELOCITY'),

  -- Entity history
  ('card_age_days',           'Card age (days)',          'Days since this card was first seen. Zero means brand new to us.', 'NUMBER', 'ENTITY'),
  ('email_age_days',          'Email age (days)',         'Days since this email was first seen.', 'NUMBER', 'ENTITY'),
  ('account_age_days',        'Account age (days)',       'Days since the customer account was first seen.', 'NUMBER', 'ENTITY'),
  ('card_seen_count',         'Card transaction count',   'Lifetime count for this card with us. High counts on a good history should REDUCE the score.', 'NUMBER', 'ENTITY'),

  -- Shared attributes: the cross-account signal
  ('cards_per_account_30d',   'Cards on this account, 30d','Many cards on one account suggests stolen card testing.', 'NUMBER', 'LINK'),
  ('accounts_per_card_30d',   'Accounts on this card, 30d','One card across many accounts is the strongest single fraud-ring signal.', 'NUMBER', 'LINK'),
  ('accounts_per_device_30d', 'Accounts on this device, 30d','One device operating many accounts is one person wearing many masks.', 'NUMBER', 'LINK'),
  ('emails_per_device_30d',   'Emails on this device, 30d','Same shape as above, seen from the email side.', 'NUMBER', 'LINK'),

  -- Lists
  ('on_deny_list',            'On deny list',             'Hard block regardless of every other signal.', 'BOOLEAN', 'LIST'),
  ('on_allow_list',           'On allow list',            'Known-good, e.g. a verified corporate buyer.', 'BOOLEAN', 'LIST'),
  ('on_watch_list',           'On watch list',            'Not blocked, but scored up and worth reviewing.', 'BOOLEAN', 'LIST');
