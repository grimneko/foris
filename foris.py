#!/usr/bin/env python
# Foris - web administration interface for OpenWrt based on NETCONF
# Copyright (C) 2013 CZ.NIC, z.s.p.o. <http://www.nic.cz>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

# builtins
import gettext
import logging
import os
import sys

# 3rd party
from beaker.middleware import SessionMiddleware
import bottle
from bottle_i18n import I18NMiddleware, I18NPlugin, i18n_defaults
from ncclient.operations import TimeoutExpiredError, RPCError

# local
from nuci import client, filters
from nuci.modules.uci_raw import Uci, Config, Section, Option
from utils import redirect_unauthenticated, is_safe_redirect, is_user_authenticated
from utils.bottle_csrf import update_csrf_token, CSRFValidationError
from utils import messages
from utils.reporting_middleware import ReportingMiddleware
from utils.routing import reverse


logger = logging.getLogger("foris")

BASE_DIR = os.path.dirname(__file__)

# internationalization
i18n_defaults(bottle.SimpleTemplate, bottle.request)
bottle.SimpleTemplate.defaults['trans'] = lambda msgid: bottle.request.app._(msgid)  # workaround
DEFAULT_LANGUAGE = 'cs'
translations = {
    'cs': gettext.translation("messages", os.path.join(BASE_DIR, "locale"),
                              languages=['cs'], fallback=True),
    'en': gettext.translation("messages", os.path.join(BASE_DIR, "locale"),
                              languages=['en'], fallback=True)
}
ugettext = lambda x: translations[bottle.request.app.lang].ugettext(x)
_ = ugettext

# template defaults
# this is not really straight-forward, check for user_authenticated() (with brackets) in template,
# because bool(user_authenticated) is always True - it means bool(<function ...>)
bottle.SimpleTemplate.defaults["user_authenticated"] =\
    lambda: bottle.request.environ["beaker.session"].get("user_authenticated")
bottle.SimpleTemplate.defaults["request"] = bottle.request
bottle.SimpleTemplate.defaults["url"] = lambda name, **kwargs: reverse(name, **kwargs)
bottle.SimpleTemplate.defaults["static"] = lambda filename, *args: reverse("static", filename=filename) % args

# messages
messages.set_template_defaults(bottle.SimpleTemplate)


def login_redirect(step_num):
    from wizard import NUM_WIZARD_STEPS
    if step_num >= NUM_WIZARD_STEPS:
        next = bottle.request.GET.get("next")
        if next and is_safe_redirect(next, bottle.request.get_header('host')):
            bottle.redirect(next)
        bottle.redirect(reverse("config_index"))
    elif step_num == 1:
        bottle.redirect(reverse("wizard_index"))
    else:
        bottle.redirect(reverse("wizard_step", number=step_num))


@bottle.route("/", name="index")
@bottle.view("index")
def index():
    session = bottle.request.environ['beaker.session']
    import wizard
    allowed_step_max = wizard.get_allowed_step_max()

    if allowed_step_max == 1:
        session["user_authenticated"] = True
        allowed_step_max = 1
    else:
        session[wizard.WizardStepMixin.next_step_allowed_key] = str(allowed_step_max)
        allowed_step_max = int(allowed_step_max)
    
    session.save()
    if session.get("user_authenticated"):
        login_redirect(allowed_step_max)

    return dict(luci_path="//%(host)s/%(path)s"
                          % {'host': bottle.request.get_header('host'), 'path': 'cgi-bin/luci'})


@bottle.route("/lang/<lang:re:\w{2}>", name="change_lang")
def change_lang(lang):
    """Change language of the interface.

    :param lang: language to set
    :raises: bottle.HTTPError if requested language is not installed
    """
    if lang in translations:
        bottle.request.app.lang = lang
        write_uci_lang(lang)
        backlink = bottle.request.GET.get('backlink')
        if backlink and is_safe_redirect(backlink, bottle.request.get_header('host')):
            bottle.redirect(backlink)
        bottle.redirect("/")
    else:
        raise bottle.HTTPError(404, "Language '%s' is not available." % lang)


def read_uci_lang(default):
    """Read interface language saved in Uci config foris.settings.lang.

    :param default: returned if no language is set in the config
    :return: language code of interface language
    """
    data = client.get(filter=filters.foris_config)
    lang = data.find_child("uci.foris.settings.lang")
    if lang is None:
        return default
    return lang.value


def write_uci_lang(lang):
    """Save interface language to foris.settings.lang.

    :param lang: language code to save
    :return: True on success, False otherwise
    """
    uci = Uci()
    # Foris language
    foris = Config("foris")
    uci.add(foris)
    server = Section("settings", "config")
    foris.add(server)
    server.add(Option("lang", lang))
    # LuCI language
    luci = Config("luci")
    uci.add(luci)
    main = Section("main", "core")
    luci.add(main)
    main.add(Option("lang", lang))
    try:
        client.edit_config(uci.get_xml())
        return True
    except (RPCError, TimeoutExpiredError):
        return False


@bottle.route("/", method="POST", name="login")
def login():
    session = bottle.request.environ["beaker.session"]
    next = bottle.request.POST.get("next")
    if _check_password(bottle.request.POST.get("password")):
        # re-generate session to prevent session fixation
        session.invalidate()
        session["user_authenticated"] = True
        update_csrf_token(save_session=False)
        session.save()
        if next and is_safe_redirect(next, bottle.request.get_header('host')):
            bottle.redirect(next)
    else:
        messages.error(_("The password you entered was not valid."))

    if next:
        redirect = "/?next=%s" % next
        if is_safe_redirect(redirect, bottle.request.get_header('host')):
            bottle.redirect(redirect)
    bottle.redirect("/")


@bottle.route("/logout", name="logout")
def logout():
    session = bottle.request.environ["beaker.session"]
    if "user_authenticated" in session:
        session.delete()
    bottle.redirect("/")


@bottle.route('/static/<filename:re:.*>', name="static")
def static(filename):
    if not bottle.DEBUG:
        logger.warning("Static files should be handled externally in production mode.")
    return bottle.static_file(filename, root=os.path.join(os.path.dirname(__file__), "static"))


def _check_password(password):
    from beaker.crypto import pbkdf2
    data = client.get(filter=filters.foris_config)
    password_hash = data.find_child("uci.foris.auth.password")
    if password_hash is None:
        # consider unset password as successful auth
        # maybe set some session variable in this case
        return True
    password_hash = password_hash.value
    # crypt automatically extracts salt and iterations from formatted pw hash
    return password_hash == pbkdf2.crypt(password, salt=password_hash)


def foris_403_handler(error):
    if isinstance(error, CSRFValidationError):
        # maybe the session expired, if so, just redirect the user
        redirect_unauthenticated()

    # otherwise display the standard error page
    bottle.app().default_error_handler(error)


@bottle.hook('after_request')
def clickjacking_protection():
    # we don't use frames at all, we can safely deny opening pages in frames
    bottle.response.headers['X-Frame-Options'] = 'DENY'


@bottle.hook('after_request')
def disable_caching(authenticated_only=True):
    """
    Hook for disabling caching.

    :param authenticated_only: apply only if user is authenticated
    """
    if not authenticated_only or authenticated_only and is_user_authenticated():
        bottle.response.headers['Cache-Control'] = "no-store, no-cache, must-revalidate, " \
                                                   "no-transform, max-age=0, post-check=0, pre-check=0"
        bottle.response.headers['Pragma'] = "no-cache"


def init_foris_app(app):
    """
    Initializes Foris application - use this method to apply properties etc.
    that should be set to main app and all the mounted apps (i.e. to the
    Bottle() instances).
    """
    app.catchall = False  # catched by LoggingMiddleware
    app.error_handler[403] = foris_403_handler
    app.add_hook('after_request', clickjacking_protection)
    app.add_hook('after_request', disable_caching)


def get_arg_parser():
    """
    Create ArgumentParser instance with Foris arguments.

    :return: instance of ArgumentParser
    """
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("-H", "--host", default="0.0.0.0")
    parser.add_argument("-p", "--port", type=int, default=8080)
    parser.add_argument("-s", "--server", choices=["wsgiref", "flup"], default="wsgiref")
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("--noauth", action="store_true",
                        help="disable authentication (available only in debug mode)")
    parser.add_argument("--nucipath", help="path to Nuci binary")
    return parser


def prepare_main_app(args):
    """
    Prepare Foris main application - i.e. apply CLI arguments, mount applications,
    install hooks and middleware etc...

    :param args: arguments received from ArgumentParser.parse_args().
    :return: bottle.app() for Foris
    """
    app = bottle.app()

    # basic and bottle settings
    template_dir = os.path.join(BASE_DIR, "templates")
    bottle.TEMPLATE_PATH.append(template_dir)
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.WARNING)
    # mount apps
    import config
    import wizard
    app.mount("/config", config.app)
    app.mount("/wizard", wizard.app)

    if args.debug:
        # "about:config" is available only in debug mode
        import uci
        # must be mounted before wrapping the app with middleware
        app.mount("/uci", uci.app)
        if args.noauth:
            logger.warning("authentication disabled")
            app.config.no_auth = True

    # set custom app attributes for main app and all mounted apps
    init_foris_app(app)
    for route in app.routes:
        if route.config.get("mountpoint"):
            mounted = route.config['mountpoint']['target']
            init_foris_app(mounted)

    # read language saved in Uci
    lang = read_uci_lang(DEFAULT_LANGUAGE)
    # i18n middleware
    if lang not in translations:
        lang = DEFAULT_LANGUAGE
    app = I18NMiddleware(app, I18NPlugin(domain="messages", lang_code=lang, default=DEFAULT_LANGUAGE, locale_dir=os.path.join(BASE_DIR, "locale")))

    # logging middleware for all mounted apps
    app = ReportingMiddleware(app, sensitive_params=("key", "password", "password_validation"))
    app.install_dump_route(bottle.app())

    if args.debug:
        # for nice debugging and profiling, try importing FireLogger support
        try:
            from firepython.middleware import FirePythonWSGI
            app = FirePythonWSGI(app)
        except ImportError:
            FirePythonWSGI = None

    # session middleware (note: session.auto does not work within Bottle)
    session_options = {
        'session.type': 'file',
        'session.data_dir': '/tmp/beaker/data',
        'session.lock_dir': '/tmp/beaker/lock',
        'session.cookie_expires': True,
        'session.timeout': 900,
        'session.auto': True,
        'session.httponly': True,
    }
    app = SessionMiddleware(app, session_options)

    # there are some threading-related errors caused by an issue in
    # Python <= 2.7.3 (Python issue #14308), this monkey-patch fixes them
    if sys.hexversion <= 0x020703f0:
        import threading
        threading._DummyThread._Thread__stop = lambda x: 42

    if args.nucipath:
        client.set_bin_path(args.nucipath)

    return app

# ---------------------------------------------------------------------------- #
#                                      MAIN                                    #
# ---------------------------------------------------------------------------- #
if __name__ == "__main__":
    parser = get_arg_parser()
    args = parser.parse_args()

    main_app = prepare_main_app(args)

    # run the right server
    if args.server == "wsgiref":
        bottle.run(app=main_app, host=args.host, port=args.port, debug=args.debug)
    elif args.server == "flup":
        # bindAddress is None - FCGI process must be spawned by the server
        bottle.run(app=main_app, server="flup", debug=args.debug, bindAddress=None)
