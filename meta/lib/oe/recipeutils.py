# Utility functions for reading and modifying recipes
#
# Some code borrowed from the OE layer index
#
# Copyright (C) 2013-2015 Intel Corporation
#

import sys
import os
import os.path
import tempfile
import textwrap
import difflib
import utils
import shutil
import re
import fnmatch
from collections import OrderedDict, defaultdict


# Help us to find places to insert values
recipe_progression = ['SUMMARY', 'DESCRIPTION', 'HOMEPAGE', 'BUGTRACKER', 'SECTION', 'LICENSE', 'LIC_FILES_CHKSUM', 'PROVIDES', 'DEPENDS', 'PR', 'PV', 'SRCREV', 'SRC_URI', 'S', 'do_fetch', 'do_unpack', 'do_patch', 'EXTRA_OECONF', 'do_configure', 'EXTRA_OEMAKE', 'do_compile', 'do_install', 'do_populate_sysroot', 'INITSCRIPT', 'USERADD', 'GROUPADD', 'PACKAGES', 'FILES', 'RDEPENDS', 'RRECOMMENDS', 'RSUGGESTS', 'RPROVIDES', 'RREPLACES', 'RCONFLICTS', 'ALLOW_EMPTY', 'do_package', 'do_deploy']
# Variables that sometimes are a bit long but shouldn't be wrapped
nowrap_vars = ['SUMMARY', 'HOMEPAGE', 'BUGTRACKER']
list_vars = ['SRC_URI', 'LIC_FILES_CHKSUM']
meta_vars = ['SUMMARY', 'DESCRIPTION', 'HOMEPAGE', 'BUGTRACKER', 'SECTION']


def pn_to_recipe(cooker, pn):
    """Convert a recipe name (PN) to the path to the recipe file"""
    import bb.providers

    if pn in cooker.recipecache.pkg_pn:
        filenames = cooker.recipecache.pkg_pn[pn]
        best = bb.providers.findBestProvider(pn, cooker.data, cooker.recipecache, cooker.recipecache.pkg_pn)
        return best[3]
    else:
        return None


def get_unavailable_reasons(cooker, pn):
    """If a recipe could not be found, find out why if possible"""
    import bb.taskdata
    taskdata = bb.taskdata.TaskData(None, skiplist=cooker.skiplist)
    return taskdata.get_reasons(pn)


def parse_recipe(fn, appendfiles, d):
    """
    Parse an individual recipe file, optionally with a list of
    bbappend files.
    """
    import bb.cache
    envdata = bb.cache.Cache.loadDataFull(fn, appendfiles, d)
    return envdata


def parse_recipe_simple(cooker, pn, d, appends=True):
    """
    Parse a recipe and optionally all bbappends that apply to it
    in the current configuration.
    """
    import bb.providers

    recipefile = pn_to_recipe(cooker, pn)
    if not recipefile:
        skipreasons = get_unavailable_reasons(cooker, pn)
        # We may as well re-use bb.providers.NoProvider here
        if skipreasons:
            raise bb.providers.NoProvider(skipreasons)
        else:
            raise bb.providers.NoProvider('Unable to find any recipe file matching %s' % pn)
    if appends:
        appendfiles = cooker.collection.get_file_appends(recipefile)
    return parse_recipe(recipefile, appendfiles, d)


def get_var_files(fn, varlist, d):
    """Find the file in which each of a list of variables is set.
    Note: requires variable history to be enabled when parsing.
    """
    envdata = parse_recipe(fn, [], d)
    varfiles = {}
    for v in varlist:
        history = envdata.varhistory.variable(v)
        files = []
        for event in history:
            if 'file' in event and not 'flag' in event:
                files.append(event['file'])
        if files:
            actualfile = files[-1]
        else:
            actualfile = None
        varfiles[v] = actualfile

    return varfiles


def patch_recipe_file(fn, values, patch=False, relpath=''):
    """Update or insert variable values into a recipe file (assuming you
       have already identified the exact file you want to update.)
       Note that some manual inspection/intervention may be required
       since this cannot handle all situations.
    """
    remainingnames = {}
    for k in values.keys():
        remainingnames[k] = recipe_progression.index(k) if k in recipe_progression else -1
    remainingnames = OrderedDict(sorted(remainingnames.iteritems(), key=lambda x: x[1]))

    with tempfile.NamedTemporaryFile('w', delete=False) as tf:
        def outputvalue(name):
            rawtext = '%s = "%s"\n' % (name, values[name])
            if name in nowrap_vars:
                tf.write(rawtext)
            elif name in list_vars:
                splitvalue = values[name].split()
                if len(splitvalue) > 1:
                    linesplit = ' \\\n' + (' ' * (len(name) + 4))
                    tf.write('%s = "%s%s"\n' % (name, linesplit.join(splitvalue), linesplit))
                else:
                    tf.write(rawtext)
            else:
                wrapped = textwrap.wrap(rawtext)
                for wrapline in wrapped[:-1]:
                    tf.write('%s \\\n' % wrapline)
                tf.write('%s\n' % wrapped[-1])

        tfn = tf.name
        with open(fn, 'r') as f:
            # First runthrough - find existing names (so we know not to insert based on recipe_progression)
            # Second runthrough - make the changes
            existingnames = []
            for runthrough in [1, 2]:
                currname = None
                for line in f:
                    if not currname:
                        insert = False
                        for k in remainingnames.keys():
                            for p in recipe_progression:
                                if re.match('^%s(_prepend|_append)*[ ?:=(]' % p, line):
                                    if remainingnames[k] > -1 and recipe_progression.index(p) > remainingnames[k] and runthrough > 1 and not k in existingnames:
                                        outputvalue(k)
                                        del remainingnames[k]
                                    break
                        for k in remainingnames.keys():
                            if re.match('^%s[ ?:=]' % k, line):
                                currname = k
                                if runthrough == 1:
                                    existingnames.append(k)
                                else:
                                    del remainingnames[k]
                                break
                        if currname and runthrough > 1:
                            outputvalue(currname)

                    if currname:
                        sline = line.rstrip()
                        if not sline.endswith('\\'):
                            currname = None
                        continue
                    if runthrough > 1:
                        tf.write(line)
                f.seek(0)
        if remainingnames:
            tf.write('\n')
            for k in remainingnames.keys():
                outputvalue(k)

    with open(tfn, 'U') as f:
        tolines = f.readlines()
    if patch:
        with open(fn, 'U') as f:
            fromlines = f.readlines()
        relfn = os.path.relpath(fn, relpath)
        diff = difflib.unified_diff(fromlines, tolines, 'a/%s' % relfn, 'b/%s' % relfn)
        os.remove(tfn)
        return diff
    else:
        with open(fn, 'w') as f:
            f.writelines(tolines)
        os.remove(tfn)
        return None

def localise_file_vars(fn, varfiles, varlist):
    """Given a list of variables and variable history (fetched with get_var_files())
    find where each variable should be set/changed. This handles for example where a
    recipe includes an inc file where variables might be changed - in most cases
    we want to update the inc file when changing the variable value rather than adding
    it to the recipe itself.
    """
    fndir = os.path.dirname(fn) + os.sep

    first_meta_file = None
    for v in meta_vars:
        f = varfiles.get(v, None)
        if f:
            actualdir = os.path.dirname(f) + os.sep
            if actualdir.startswith(fndir):
                first_meta_file = f
                break

    filevars = defaultdict(list)
    for v in varlist:
        f = varfiles[v]
        # Only return files that are in the same directory as the recipe or in some directory below there
        # (this excludes bbclass files and common inc files that wouldn't be appropriate to set the variable
        # in if we were going to set a value specific to this recipe)
        if f:
            actualfile = f
        else:
            # Variable isn't in a file, if it's one of the "meta" vars, use the first file with a meta var in it
            if first_meta_file:
                actualfile = first_meta_file
            else:
                actualfile = fn

        actualdir = os.path.dirname(actualfile) + os.sep
        if not actualdir.startswith(fndir):
            actualfile = fn
        filevars[actualfile].append(v)

    return filevars

def patch_recipe(d, fn, varvalues, patch=False, relpath=''):
    """Modify a list of variable values in the specified recipe. Handles inc files if
    used by the recipe.
    """
    varlist = varvalues.keys()
    varfiles = get_var_files(fn, varlist, d)
    locs = localise_file_vars(fn, varfiles, varlist)
    patches = []
    for f,v in locs.iteritems():
        vals = {k: varvalues[k] for k in v}
        patchdata = patch_recipe_file(f, vals, patch, relpath)
        if patch:
            patches.append(patchdata)

    if patch:
        return patches
    else:
        return None



def copy_recipe_files(d, tgt_dir, whole_dir=False, download=True):
    """Copy (local) recipe files, including both files included via include/require,
    and files referred to in the SRC_URI variable."""
    import bb.fetch2
    import oe.path

    # FIXME need a warning if the unexpanded SRC_URI value contains variable references

    uris = (d.getVar('SRC_URI', True) or "").split()
    fetch = bb.fetch2.Fetch(uris, d)
    if download:
        fetch.download()

    # Copy local files to target directory and gather any remote files
    bb_dir = os.path.dirname(d.getVar('FILE', True)) + os.sep
    remotes = []
    includes = [path for path in d.getVar('BBINCLUDED', True).split() if
                path.startswith(bb_dir) and os.path.exists(path)]
    for path in fetch.localpaths() + includes:
        # Only import files that are under the meta directory
        if path.startswith(bb_dir):
            if not whole_dir:
                relpath = os.path.relpath(path, bb_dir)
                subdir = os.path.join(tgt_dir, os.path.dirname(relpath))
                if not os.path.exists(subdir):
                    os.makedirs(subdir)
                shutil.copy2(path, os.path.join(tgt_dir, relpath))
        else:
            remotes.append(path)
    # Simply copy whole meta dir, if requested
    if whole_dir:
        shutil.copytree(bb_dir, tgt_dir)

    return remotes


def get_recipe_patches(d):
    """Get a list of the patches included in SRC_URI within a recipe."""
    patchfiles = []
    # Execute src_patches() defined in patch.bbclass - this works since that class
    # is inherited globally
    patches = bb.utils.exec_flat_python_func('src_patches', d)
    for patch in patches:
        _, _, local, _, _, parm = bb.fetch.decodeurl(patch)
        patchfiles.append(local)
    return patchfiles


def get_recipe_patched_files(d):
    """
    Get the list of patches for a recipe along with the files each patch modifies.
    Params:
        d: the datastore for the recipe
    Returns:
        a dict mapping patch file path to a list of tuples of changed files and
        change mode ('A' for add, 'D' for delete or 'M' for modify)
    """
    import oe.patch
    # Execute src_patches() defined in patch.bbclass - this works since that class
    # is inherited globally
    patches = bb.utils.exec_flat_python_func('src_patches', d)
    patchedfiles = {}
    for patch in patches:
        _, _, patchfile, _, _, parm = bb.fetch.decodeurl(patch)
        striplevel = int(parm['striplevel'])
        patchedfiles[patchfile] = oe.patch.PatchSet.getPatchedFiles(patchfile, striplevel, os.path.join(d.getVar('S', True), parm.get('patchdir', '')))
    return patchedfiles


def validate_pn(pn):
    """Perform validation on a recipe name (PN) for a new recipe."""
    reserved_names = ['forcevariable', 'append', 'prepend', 'remove']
    if not re.match('[0-9a-z-.]+', pn):
        return 'Recipe name "%s" is invalid: only characters 0-9, a-z, - and . are allowed' % pn
    elif pn in reserved_names:
        return 'Recipe name "%s" is invalid: is a reserved keyword' % pn
    elif pn.startswith('pn-'):
        return 'Recipe name "%s" is invalid: names starting with "pn-" are reserved' % pn
    return ''


def get_bbappend_path(d, destlayerdir, wildcardver=False):
    """Determine how a bbappend for a recipe should be named and located within another layer"""

    import bb.cookerdata

    destlayerdir = os.path.abspath(destlayerdir)
    recipefile = d.getVar('FILE', True)
    recipefn = os.path.splitext(os.path.basename(recipefile))[0]
    if wildcardver and '_' in recipefn:
        recipefn = recipefn.split('_', 1)[0] + '_%'
    appendfn = recipefn + '.bbappend'

    # Parse the specified layer's layer.conf file directly, in case the layer isn't in bblayers.conf
    confdata = d.createCopy()
    confdata.setVar('BBFILES', '')
    confdata.setVar('LAYERDIR', destlayerdir)
    destlayerconf = os.path.join(destlayerdir, "conf", "layer.conf")
    confdata = bb.cookerdata.parse_config_file(destlayerconf, confdata)

    origlayerdir = find_layerdir(recipefile)
    if not origlayerdir:
        return (None, False)
    # Now join this to the path where the bbappend is going and check if it is covered by BBFILES
    appendpath = os.path.join(destlayerdir, os.path.relpath(os.path.dirname(recipefile), origlayerdir), appendfn)
    closepath = ''
    pathok = True
    for bbfilespec in confdata.getVar('BBFILES', True).split():
        if fnmatch.fnmatchcase(appendpath, bbfilespec):
            # Our append path works, we're done
            break
        elif bbfilespec.startswith(destlayerdir) and fnmatch.fnmatchcase('test.bbappend', os.path.basename(bbfilespec)):
            # Try to find the longest matching path
            if len(bbfilespec) > len(closepath):
                closepath = bbfilespec
    else:
        # Unfortunately the bbappend layer and the original recipe's layer don't have the same structure
        if closepath:
            # bbappend layer's layer.conf at least has a spec that picks up .bbappend files
            # Now we just need to substitute out any wildcards
            appendsubdir = os.path.relpath(os.path.dirname(closepath), destlayerdir)
            if 'recipes-*' in appendsubdir:
                # Try to copy this part from the original recipe path
                res = re.search('/recipes-[^/]+/', recipefile)
                if res:
                    appendsubdir = appendsubdir.replace('/recipes-*/', res.group(0))
            # This is crude, but we have to do something
            appendsubdir = appendsubdir.replace('*', recipefn.split('_')[0])
            appendsubdir = appendsubdir.replace('?', 'a')
            appendpath = os.path.join(destlayerdir, appendsubdir, appendfn)
        else:
            pathok = False
    return (appendpath, pathok)


def bbappend_recipe(rd, destlayerdir, srcfiles, install=None, wildcardver=False, machine=None, extralines=None, removevalues=None):
    """
    Writes a bbappend file for a recipe
    Parameters:
        rd: data dictionary for the recipe
        destlayerdir: base directory of the layer to place the bbappend in
            (subdirectory path from there will be determined automatically)
        srcfiles: dict of source files to add to SRC_URI, where the value
            is the full path to the file to be added, and the value is the
            original filename as it would appear in SRC_URI or None if it
            isn't already present. You may pass None for this parameter if
            you simply want to specify your own content via the extralines
            parameter.
        install: dict mapping entries in srcfiles to a tuple of two elements:
            install path (*without* ${D} prefix) and permission value (as a
            string, e.g. '0644').
        wildcardver: True to use a % wildcard in the bbappend filename, or
            False to make the bbappend specific to the recipe version.
        machine:
            If specified, make the changes in the bbappend specific to this
            machine. This will also cause PACKAGE_ARCH = "${MACHINE_ARCH}"
            to be added to the bbappend.
        extralines:
            Extra lines to add to the bbappend. This may be a dict of name
            value pairs, or simply a list of the lines.
        removevalues:
            Variable values to remove - a dict of names/values.
    """

    if not removevalues:
        removevalues = {}

    # Determine how the bbappend should be named
    appendpath, pathok = get_bbappend_path(rd, destlayerdir, wildcardver)
    if not appendpath:
        bb.error('Unable to determine layer directory containing %s' % recipefile)
        return (None, None)
    if not pathok:
        bb.warn('Unable to determine correct subdirectory path for bbappend file - check that what %s adds to BBFILES also matches .bbappend files. Using %s for now, but until you fix this the bbappend will not be applied.' % (os.path.join(destlayerdir, 'conf', 'layer.conf'), os.path.dirname(appendpath)))

    appenddir = os.path.dirname(appendpath)
    bb.utils.mkdirhier(appenddir)

    # FIXME check if the bbappend doesn't get overridden by a higher priority layer?

    layerdirs = [os.path.abspath(layerdir) for layerdir in rd.getVar('BBLAYERS', True).split()]
    if not os.path.abspath(destlayerdir) in layerdirs:
        bb.warn('Specified layer is not currently enabled in bblayers.conf, you will need to add it before this bbappend will be active')

    bbappendlines = []
    if extralines:
        if isinstance(extralines, dict):
            for name, value in extralines.iteritems():
                bbappendlines.append((name, '=', value))
        else:
            # Do our best to split it
            for line in extralines:
                if line[-1] == '\n':
                    line = line[:-1]
                splitline = line.split(maxsplit=2)
                if len(splitline) == 3:
                    bbappendlines.append(tuple(splitline))
                else:
                    raise Exception('Invalid extralines value passed')

    def popline(varname):
        for i in xrange(0, len(bbappendlines)):
            if bbappendlines[i][0] == varname:
                line = bbappendlines.pop(i)
                return line
        return None

    def appendline(varname, op, value):
        for i in xrange(0, len(bbappendlines)):
            item = bbappendlines[i]
            if item[0] == varname:
                bbappendlines[i] = (item[0], item[1], item[2] + ' ' + value)
                break
        else:
            bbappendlines.append((varname, op, value))

    destsubdir = rd.getVar('PN', True)
    if srcfiles:
        bbappendlines.append(('FILESEXTRAPATHS_prepend', ':=', '${THISDIR}/${PN}:'))

    appendoverride = ''
    if machine:
        bbappendlines.append(('PACKAGE_ARCH', '=', '${MACHINE_ARCH}'))
        appendoverride = '_%s' % machine
    copyfiles = {}
    if srcfiles:
        instfunclines = []
        for newfile, origsrcfile in srcfiles.iteritems():
            srcfile = origsrcfile
            srcurientry = None
            if not srcfile:
                srcfile = os.path.basename(newfile)
                srcurientry = 'file://%s' % srcfile
                # Double-check it's not there already
                # FIXME do we care if the entry is added by another bbappend that might go away?
                if not srcurientry in rd.getVar('SRC_URI', True).split():
                    if machine:
                        appendline('SRC_URI_append%s' % appendoverride, '=', ' ' + srcurientry)
                    else:
                        appendline('SRC_URI', '+=', srcurientry)
            copyfiles[newfile] = srcfile
            if install:
                institem = install.pop(newfile, None)
                if institem:
                    (destpath, perms) = institem
                    instdestpath = replace_dir_vars(destpath, rd)
                    instdirline = 'install -d ${D}%s' % os.path.dirname(instdestpath)
                    if not instdirline in instfunclines:
                        instfunclines.append(instdirline)
                    instfunclines.append('install -m %s ${WORKDIR}/%s ${D}%s' % (perms, os.path.basename(srcfile), instdestpath))
        if instfunclines:
            bbappendlines.append(('do_install_append%s()' % appendoverride, '', instfunclines))

    bb.note('Writing append file %s' % appendpath)

    if os.path.exists(appendpath):
        # Work around lack of nonlocal in python 2
        extvars = {'destsubdir': destsubdir}

        def appendfile_varfunc(varname, origvalue, op, newlines):
            if varname == 'FILESEXTRAPATHS_prepend':
                if origvalue.startswith('${THISDIR}/'):
                    popline('FILESEXTRAPATHS_prepend')
                    extvars['destsubdir'] = rd.expand(origvalue.split('${THISDIR}/', 1)[1].rstrip(':'))
            elif varname == 'PACKAGE_ARCH':
                if machine:
                    popline('PACKAGE_ARCH')
                    return (machine, None, 4, False)
            elif varname.startswith('do_install_append'):
                func = popline(varname)
                if func:
                    instfunclines = [line.strip() for line in origvalue.strip('\n').splitlines()]
                    for line in func[2]:
                        if not line in instfunclines:
                            instfunclines.append(line)
                    return (instfunclines, None, 4, False)
            else:
                splitval = origvalue.split()
                changed = False
                removevar = varname
                if varname in ['SRC_URI', 'SRC_URI_append%s' % appendoverride]:
                    removevar = 'SRC_URI'
                    line = popline(varname)
                    if line:
                        if line[2] not in splitval:
                            splitval.append(line[2])
                            changed = True
                else:
                    line = popline(varname)
                    if line:
                        splitval = [line[2]]
                        changed = True

                if removevar in removevalues:
                    remove = removevalues[removevar]
                    if isinstance(remove, basestring):
                        if remove in splitval:
                            splitval.remove(remove)
                            changed = True
                    else:
                        for removeitem in remove:
                            if removeitem in splitval:
                                splitval.remove(removeitem)
                                changed = True

                if changed:
                    newvalue = splitval
                    if len(newvalue) == 1:
                        # Ensure it's written out as one line
                        if '_append' in varname:
                            newvalue = ' ' + newvalue[0]
                        else:
                            newvalue = newvalue[0]
                    if not newvalue and (op in ['+=', '.='] or '_append' in varname):
                        # There's no point appending nothing
                        newvalue = None
                    if varname.endswith('()'):
                        indent = 4
                    else:
                        indent = -1
                    return (newvalue, None, indent, True)
            return (origvalue, None, 4, False)

        varnames = [item[0] for item in bbappendlines]
        if removevalues:
            varnames.extend(removevalues.keys())

        with open(appendpath, 'r') as f:
            (updated, newlines) = bb.utils.edit_metadata(f, varnames, appendfile_varfunc)

        destsubdir = extvars['destsubdir']
    else:
        updated = False
        newlines = []

    if bbappendlines:
        for line in bbappendlines:
            if line[0].endswith('()'):
                newlines.append('%s {\n    %s\n}\n' % (line[0], '\n    '.join(line[2])))
            else:
                newlines.append('%s %s "%s"\n\n' % line)
        updated = True

    if updated:
        with open(appendpath, 'w') as f:
            f.writelines(newlines)

    if copyfiles:
        if machine:
            destsubdir = os.path.join(destsubdir, machine)
        for newfile, srcfile in copyfiles.iteritems():
            filedest = os.path.join(appenddir, destsubdir, os.path.basename(srcfile))
            if os.path.abspath(newfile) != os.path.abspath(filedest):
                bb.note('Copying %s to %s' % (newfile, filedest))
                bb.utils.mkdirhier(os.path.dirname(filedest))
                shutil.copyfile(newfile, filedest)

    return (appendpath, os.path.join(appenddir, destsubdir))


def find_layerdir(fn):
    """ Figure out relative path to base of layer for a file (e.g. a recipe)"""
    pth = os.path.dirname(fn)
    layerdir = ''
    while pth:
        if os.path.exists(os.path.join(pth, 'conf', 'layer.conf')):
            layerdir = pth
            break
        pth = os.path.dirname(pth)
    return layerdir


def replace_dir_vars(path, d):
    """Replace common directory paths with appropriate variable references (e.g. /etc becomes ${sysconfdir})"""
    dirvars = {}
    for var in d:
        if var.endswith('dir') and var.lower() == var:
            value = d.getVar(var, True)
            if value.startswith('/') and not '\n' in value:
                dirvars[value] = var
    for dirpath in sorted(dirvars.keys(), reverse=True):
        path = path.replace(dirpath, '${%s}' % dirvars[dirpath])
    return path

