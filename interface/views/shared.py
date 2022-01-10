# Copyright: 2019, NLnet Labs and the Internet.nl contributors
# SPDX-License-Identifier: Apache-2.0
import random
import re
import time
from datetime import datetime
from timeit import default_timer as timer
from urllib.parse import urlparse

import idna
import yaml
from celery import shared_task
from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpResponseRedirect
from django.shortcuts import render
from django.utils import timezone
from django.utils.translation import ugettext as _

import unbound
from interface import redis_id

ub_ctx = unbound.ub_ctx()
if hasattr(settings, "ENABLE_INTEGRATION_TEST") and settings.ENABLE_INTEGRATION_TEST:
    ub_ctx.debuglevel(2)
    ub_ctx.config(settings.IT_UNBOUND_CONFIG_PATH)
    ub_ctx.set_fwd(settings.IT_UNBOUND_FORWARD_IP)
# XXX: Remove for now; inconsistency with applying settings on celery.
# ub_ctx.set_async(True)
if settings.ENABLE_BATCH and settings.CENTRAL_UNBOUND:
    ub_ctx.set_fwd("{}".format(settings.CENTRAL_UNBOUND))

# See: https://stackoverflow.com/a/53875771 for a good summary of the various
# RFCs and other rulings that combine to define what is a valid domain name.
# Of particular note are xn-- which is used for internationalized TLDs, and
# the rejection of digits in the TLD if not xn--. Digits in the last label
# were legal under the original RFC-1035 but not according to the "ICANN
# Application Guidebook for new TLDs (June 2012)" which stated that "The
# ASCII label must consist entirely of letters (alphabetic characters a-z)".
regex_dname = r"^([a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?\.)+" "([a-zA-Z]{2,63}|xn--[a-zA-Z0-9]+)$"

HOME_STATS_LOCK_ID = redis_id.home_stats_lock.id
HOME_STATS_LOCK_TTL = redis_id.home_stats_lock.ttl


def execsql(sql):
    """
    Execute raw SQL query.

    """
    with connection.cursor() as cursor:
        cursor.execute(sql, [])
        row = cursor.fetchone()
    return row[0]


def validate_dname(dname):
    """
    Validates a domain name and return canonical version.

    If *dname* does not contain a valid domain name, returns `None`.

    """
    try:
        urlp = urlparse(dname)
        if urlp.netloc != "":
            dname = urlp.netloc
        elif urlp.path != "":
            dname = urlp.path

        # Convert to punnycode
        dname = idna.encode(dname).decode("ascii")

        if re.match(regex_dname, dname):
            return dname
        else:
            return None
    except (UnicodeError, ValueError, idna.IDNAError):
        return None


def proberesults(request, probe, dname):
    """
    Check if a probe has finished and also return the results.

    """
    url = dname.lower()
    done, _ = probe.raw_results(url, get_client_ip(request))
    if done:
        results = probe.rated_results(url)
    else:
        results = dict(done=False)
    return results


def probestatus(request, probe, dname):
    """
    Check if a probe has finished.

    """
    url = dname.lower()
    return probe.check_results(url, get_client_ip(request))


def probestatuses(request, dname, probes):
    """
    Return the statuses (done or not) of the probes.

    """
    statuses = []
    for probe in probes:
        results = dict(name=probe.name)
        results["done"] = probestatus(request, probe, dname)
        statuses.append(results)
    return statuses


def get_client_ip(request):
    """
    Get the client's IP address.

    If the server is proxied use the X_FORWARDED_FOR content.

    """
    if settings.DJANGO_IS_PROXIED:
        ip = request.META.get("HTTP_X_FORWARDED_FOR", None)
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


def pretty_domain_name(dname):
    """
    Return a pretty printable domain name.

    If *dname* is in punnycode, decode it.

    """
    try:
        pretty = dname
        pretty = idna.decode(dname.encode("ascii"))
    except (UnicodeError, idna.IDNAError):
        pass
    return pretty


# Page calling/displaying JSON API
# URL: /(site|domain)/<dname>
def process(request, dname, template, probes, pageclass, pagetitle):
    addr = dname.lower()
    sorted_probes = probes.getset()
    done_count = 0
    no_javascript_redirect = request.path
    # Start the tests.
    # Also check if every test is done. In case of no-javascript we redirect
    # either to results or the same page.
    for probe in sorted_probes:
        done, results = probe.raw_results(addr, get_client_ip(request))
        if done:
            done_count += 1
    if done_count >= len(sorted_probes):
        no_javascript_redirect = "results"

    prettyaddr = pretty_domain_name(dname)

    return render(
        request,
        template,
        dict(
            addr=addr,
            prettyaddr=prettyaddr,
            pageclass=pageclass,
            pagetitle="{} {}".format(_(pagetitle), prettyaddr),
            probes=sorted_probes,
            no_javascript_redirect=no_javascript_redirect,
            javascript_retries=get_javascript_retries(),
            javascript_timeout=settings.JAVASCRIPT_TIMEOUT * 1000,
        ),
    )


def get_javascript_retries():
    """
    Get number of javascript retries we are allowed to do before we reach
    the CACHE_TTL. Prevents infinitely registering slow tests.

    """
    return max(int(settings.CACHE_TTL / settings.JAVASCRIPT_TIMEOUT) - 2, 0)


def add_registrar_to_report(report):
    """
    Add the registrar information from the DNSSEC test to the report.

    """
    if report.registrar:
        return

    if isinstance(report.dnssec.report, dict) and report.dnssec.report.get("dnssec_exists"):
        registrar = report.dnssec.report["dnssec_exists"]["tech_data"]
        registrar = registrar[0][1]
        report.registrar = registrar
        report.save()


def add_score_to_report(report, score):
    """
    Add score to report if there is none.

    """
    if report.score is None:
        report.score = score
        report.save()


def get_hof_cache(cache_id, count):
    cached_data = cache.get(cache_id, None)
    if cached_data is None:
        return "…", 0, []
    return (cached_data["date"], cached_data["count"], cached_data["data"][:count])


def get_hof_champions(count=100000):
    return get_hof_cache(redis_id.hof_champions.id, count)


def get_hof_web(count=100000):
    return get_hof_cache(redis_id.hof_web.id, count)


def get_hof_mail(count=100000):
    return get_hof_cache(redis_id.hof_mail.id, count)


def get_hof_manual(manual):
    hof_entries = []
    try:
        with open(settings.MANUAL_HOF[manual]["entries_file"], "r") as f:
            hof_entries = yaml.load(f, Loader=yaml.Loader)
    except Exception:
        pass
    random.shuffle(hof_entries)
    return (len(hof_entries), hof_entries)


def get_retest_time(report):
    time_delta = timezone.make_aware(datetime.now()) - report.timestamp
    return int(max(0, settings.CACHE_TTL - time_delta.total_seconds()))


def ub_resolve_with_timeout(qname, qtype, rr_class, timeout):
    def ub_callback(data, status, result):
        if status == 0 and result.havedata:
            data["data"] = result.data
        data["nxdomain"] = result.nxdomain
        data["rcode"] = result.rcode
        data["done"] = True

    cb_data = dict(done=False)
    retval, async_id = ub_ctx.resolve_async(qname, cb_data, ub_callback, qtype, rr_class)

    start = timer()
    while retval == 0 and not cb_data["done"]:
        time.sleep(0.1)
        retval = ub_ctx.process()
        if timer() - start > timeout:
            if async_id:
                ub_ctx.cancel(async_id)
            cb_data["done"] = True
    return cb_data


def get_valid_domain_web(dname, timeout=5):
    dname = validate_dname(dname)
    if dname is None:
        return None

    for qtype in (unbound.RR_TYPE_A, unbound.RR_TYPE_AAAA):
        cb_data = ub_resolve_with_timeout(dname, qtype, unbound.RR_CLASS_IN, timeout)
        if cb_data.get("data") and cb_data["data"].data:
            return dname

    return None


def get_valid_domain_mail(mailaddr, timeout=5):
    dname = validate_dname(mailaddr)
    if dname is None:
        return None

    cb_data = ub_resolve_with_timeout(dname, unbound.RR_TYPE_SOA, unbound.RR_CLASS_IN, timeout)

    if cb_data.get("nxdomain") and cb_data["nxdomain"]:
        return None

    return dname


def redirect_invalid_domain(request, domain_type):
    if domain_type == "domain":
        return HttpResponseRedirect("/test-site/?invalid")
    elif domain_type == "mail":
        return HttpResponseRedirect("/test-mail/?invalid")
    else:
        return HttpResponseRedirect("/")


@shared_task(
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_LOW,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_LOW,
    ignore_result=True,
)
def run_stats_queries():
    """
    Run the queries for the home page statistics and save the results in redis.
    """

    query = """
         select
            count(distinct r.domain) as count
        from
            checks_domaintestreport as r
        inner join
            (
                select
                    domain,
                    max(timestamp) as timestamp
                from
                    checks_domaintestreport
                group by
                    domain
            )          as rmax
                on r.domain = rmax.domain
                and r.timestamp = rmax.timestamp
    """
    statswebsite = execsql(query)
    statswebsitegood = get_hof_web(count=1)[1]
    statswebsitebad = max(statswebsite - statswebsitegood, 0)

    query = """
        select
            count(distinct r.domain) as count
        from
            checks_mailtestreport as r
        inner join
            (
                select
                    domain,
                    max(timestamp) as timestamp
                from
                    checks_mailtestreport
                group by
                    domain
            ) as rmax
                on r.domain = rmax.domain
                and r.timestamp = rmax.timestamp
    """
    statsmail = execsql(query)
    statsmailgood = get_hof_mail(count=1)[1]
    statsmailbad = max(statsmail - statsmailgood, 0)

    query = """
        select
            count(distinct coalesce(ipv4_addr,
            ipv6_addr)) as count
        from
            checks_connectiontest as r
        inner join
            (
                select
                    coalesce(ipv4_addr,
                    ipv6_addr) as source,
                    max(timestamp) as timestamp
                from
                    checks_connectiontest
                where
                    finished = true
                group by
                    coalesce(ipv4_addr,
                    ipv6_addr)
            ) as rmax
                on coalesce(r.ipv4_addr,
            r.ipv6_addr) = rmax.source
        where
            finished = true
    """
    statsconnection = execsql(query)

    query = """
        select
            count(distinct coalesce(ipv4_addr,
            ipv6_addr)) as count
        from
            checks_connectiontest as r
        inner join
            (
                select
                    coalesce(ipv4_addr,
                    ipv6_addr) as      source,
                    max(timestamp) as timestamp
                from
                    checks_connectiontest
                where
                    finished = true
                group by
                    coalesce(ipv4_addr,
                    ipv6_addr)
            ) as rmax
                on coalesce(r.ipv4_addr,
            r.ipv6_addr) = rmax.source
        where
            finished = true
            and score_dnssec = 100
            and score_ipv6 = 100
    """
    statsconnectiongood = execsql(query)
    statsconnectionbad = max(statsconnection - statsconnectiongood, 0)

    cache_id = redis_id.home_stats_data.id
    cache_ttl = redis_id.home_stats_data.ttl
    cache.set(cache_id.format("statswebsite"), statswebsite, cache_ttl)
    cache.set(cache_id.format("statswebsitegood"), statswebsitegood, cache_ttl)
    cache.set(cache_id.format("statswebsitebad"), statswebsitebad, cache_ttl)
    cache.set(cache_id.format("statsmail"), statsmail, cache_ttl)
    cache.set(cache_id.format("statsmailgood"), statsmailgood, cache_ttl)
    cache.set(cache_id.format("statsmailbad"), statsmailbad, cache_ttl)
    cache.set(cache_id.format("statsconnection"), statsconnection, cache_ttl)
    cache.set(cache_id.format("statsconnectiongood"), statsconnectiongood, cache_ttl)
    cache.set(cache_id.format("statsconnectionbad"), statsconnectionbad, cache_ttl)


@shared_task(
    soft_time_limit=settings.SHARED_TASK_SOFT_TIME_LIMIT_LOW,
    time_limit=settings.SHARED_TASK_TIME_LIMIT_LOW,
    ignore_result=True,
)
def update_running_status(results):
    """
    Signal that the queries for the home page statistics finished running.

    """
    cache_id = HOME_STATS_LOCK_ID
    cache_ttl = HOME_STATS_LOCK_TTL
    if cache.get(cache_id):
        cache.set(cache_id, False, cache_ttl)


def update_base_stats():
    """
    If the queries for the home page statistics are not already running,
    run them.

    This is done to:
    - Not having to run the queries for every visit;
    - Avoid queueing unnecessary tasks.

    """
    cache_id = HOME_STATS_LOCK_ID
    cache_ttl = HOME_STATS_LOCK_TTL
    if not cache.get(cache_id):
        cache.set(cache_id, True, cache_ttl)
        task_set = run_stats_queries.s() | update_running_status.s()
        task_set()
