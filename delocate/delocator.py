""" Routines to manipulate dynamic libraries in trees
"""

from __future__ import division, print_function

import os
from os.path import (join as pjoin, dirname, basename, exists, isdir, abspath,
                     relpath)
import zipfile
import shutil

from .tools import add_rpath, set_install_name, tree_libs
from .tmpdirs import InTemporaryDirectory

class DelocationError(Exception):
    pass


def delocate_tree_libs(lib_dict, lib_path, root_path):
    """ Move needed libraries in `lib_dict` into `lib_path`

    `lib_dict` has keys naming libraries required by the files in the
    corresponding value.  Call the keys, "required libs".  Call the values
    "requiring objects".

    Copy all the required libs to `lib_path`.  Fix up the rpaths and install
    names in the requiring objects to point to these new copies.

    Exception: required libs within the directory tree pointed to by
    `root_path` stay where they are, but we modify requiring objects to use
    relative paths to these libraries.

    Parameters
    ----------
    lib_dict : dict
        Dictionary with (key, value) pairs of (required library, set of files
        in tree depending on that library)
    lib_path : str
        Path in which to store copies of libs referred to in keys of
        `lib_dict`.  Assumed to exist
    root_path : str, optional
        Root directory of tree analyzed in `lib_dict`.  Any required
        library within the subtrees of `root_path` does not get copied, but
        libraries linking to it have links adjusted to use relative path to
        this library.

    Returns
    -------
    copied_libs : set
        Set of names of libraries copied into `lib_path`. These are the
        original names from the keys of `lib_dict`
    """
    copied_libs = set()
    delocated_libs = set()
    copied_basenames = set()
    # Test for errors first to avoid getting half-way through changing the tree
    for required, requirings in lib_dict.items():
        if required.startswith('@'): # assume @rpath etc are correct
            continue
        r_ed_base = basename(required)
        if relpath(required, root_path).startswith('..'):
            # Not local, plan to copy
            if r_ed_base in copied_basenames:
                raise DelocationError('Already planning to copy library with '
                                      'same basename as: ' + r_ed_base)
            if not exists(required):
                raise DelocationError('library "{0}" does not exist'.format(
                    required))
            copied_libs.add(required)
            copied_basenames.add(r_ed_base)
        else: # Is local, plan to set relative loader_path
            delocated_libs.add(required)
    # Modify in place now that we've checked for errors
    rpathed = set()
    for required in copied_libs:
        shutil.copy2(required, lib_path)
        # Set rpath and install names for this copied library
        for requiring in lib_dict[required]:
            if requiring not in rpathed:
                req_rel = relpath(lib_path, dirname(requiring))
                add_rpath(requiring, '@loader_path/' + req_rel)
                rpathed.add(requiring)
            set_install_name(requiring, required,
                             '@rpath/' + basename(required))
    for required in delocated_libs:
        # Set relative path for local library
        for requiring in lib_dict[required]:
            req_rel = relpath(required, dirname(requiring))
            set_install_name(requiring, required,
                             '@loader_path/' + req_rel)
    return copied_libs


def copy_recurse(lib_path, copy_filt_func = None, copied_libs = None):
    """ Analyze `lib_path` for library dependencies and copy libraries

    `lib_path` is a directory containing libraries.  The libraries might
    themselves have dependencies.  This function analyzes the dependencies and
    copies library dependencies that match the filter `copy_filt_func`. It alos
    adjusts the depending libraries to use the copy. It keeps iterating over
    `lib_path` until all matching dependencies (of dependencies of
    dependencies ...) have been copied.

    Parameters
    ----------
    lib_path : str
        Directory containing libraries
    copy_filt_func : None or callable, optional
        If None, copy any library that found libraries depend on.  If callable,
        called on each depended library name; copy where
        ``copy_filt_func(libname)`` is True, don't copy otherwise
    copied_libs : None or set, optional
        Set of names of libraries already copied into `lib_path`. We need this
        so we can avoid copying libraries we've already copied.
    """
    if copied_libs is None:
        copied_libs = set()
    else:
        copied_libs = set(copied_libs)
    done = False
    while not done:
        new_copied = _copy_required(lib_path, copy_filt_func, copied_libs)
        copied_libs = copied_libs | new_copied
        done = len(new_copied) == 0
    return


def _copy_required(lib_path, copy_filt_func, copied_libs):
    """ Copy libraries required for files in `lib_path` to `lib_path`

    This is one pass of ``copy_recurse``

    Parameters
    ----------
    lib_path : str
        Directory containing libraries
    copy_filt_func : None or callable, optional
        If None, copy any library that found libraries depend on.  If callable,
        called on each library name; copy where ``copy_filt_func(libname)`` is
        True, don't copy otherwise
    copied_libs : None or set, optional
        Set of names of libraries already copied into `lib_path`, so we can use these
        library copies instead of the originals

    Returns
    -------
    new_copied : set
        Set giving new libraries copied in this run
    """
    lib_dict = tree_libs(lib_path)
    new_copied = set()
    for required, requirings in lib_dict.items():
        if not copy_filt_func is None and not copy_filt_func(required):
            continue
        if required.startswith('@'):
            continue
        if not required in copied_libs:
            shutil.copy2(required, lib_path)
            new_copied.add(required)
        for requiring in requirings:
            set_install_name(requiring,
                             required,
                             '@loader_path/' + basename(required))
    return new_copied


def _dylibs_only(filename):
    return (filename.endswith('.so') or
            filename.endswith('.dylib'))


def _not_sys_libs(libname):
    return not (libname.startswith('/usr/lib') or
                libname.startswith('/System'))


def delocate_path(tree_path, lib_path,
                  lib_filt_func = _dylibs_only,
                  copy_filt_func = _not_sys_libs):
    """ Copy required libraries for files in `tree_path` into `lib_path`

    Parameters
    ----------
    tree_path : str
        Root path of tree to search for required libraries
    lib_path : str
        Directory into which we copy required libraries
    lib_filt_func : None or callable, optional
        If None, inspect all files for dependencies on dynamic libraries. If
        callable, accepts filename as argument, returns True if we should
        inspect the file, False otherwise. Default is callable rejecting all
        but files ending in ``.so`` or ``.dylib``.
    copy_filt_func : None or callable, optional
        If callable, called on each library name detected as a dependency; copy
        where ``copy_filt_func(libname)`` is True, don't copy otherwise.
        Default is callable rejecting only libraries beginning with
        ``/usr/lib`` or ``/System``.  None means copy all libraries. This will
        usually end up copying large parts of the system run-time.
    """
    if not exists(lib_path):
        os.makedirs(lib_path)
    lib_dict = tree_libs(tree_path, lib_filt_func)
    if not copy_filt_func is None:
        lib_dict = dict((key, value) for key, value in lib_dict.items()
                        if copy_filt_func(key))
    copied = delocate_tree_libs(lib_dict, lib_path, tree_path)
    copy_recurse(lib_path, copy_filt_func, copied)


def _unpack_zip_to(zip_fname, out_path):
    z = zipfile.ZipFile(zip_fname, 'r')
    z.extractall(out_path)
    z.close()


def _pack_zip_to(in_path, zip_fname):
    z = zipfile.ZipFile(zip_fname, 'w')
    for root, dirs, files in os.walk(in_path):
        for file in files:
            fname = pjoin(root, file)
            out_fname = relpath(fname, in_path)
            z.write(os.path.join(root, file), out_fname)
    z.close()


def delocate_wheel(wheel_fname, lib_sdir = '.dylibs',
                   lib_filt_func = _dylibs_only,
                   copy_filt_func = _not_sys_libs):
    """ Update wheel by copying required libraries to `lib_sdir` in wheel

    Create `lib_sdir` in wheel tree only if we are copying one or more
    libraries.

    Overwrite the wheel `wheel_fname` in-place.

    Parameters
    ----------
    wheel_fname : str
        Filename of wheel to process
    lib_sdir : str, optional
        Subdirectory name in wheel package directory (or directories) to store
        needed libraries.
    lib_filt_func : None or callable, optional
        If None, inspect all files for dependencies on dynamic libraries. If
        callable, accepts filename as argument, returns True if we should
        inspect the file, False otherwise. Default is callable rejecting all
        but files ending in ``.so`` or ``.dylib``.
    copy_filt_func : None or callable, optional
        If callable, called on each library name detected as a dependency; copy
        where ``copy_filt_func(libname)`` is True, don't copy otherwise.
        Default is callable rejecting only libraries beginning with
        ``/usr/lib`` or ``/System``.  None means copy all libraries. This will
        usually end up copying large parts of the system run-time.
    """
    wheel_fname = abspath(wheel_fname)
    with InTemporaryDirectory():
        _unpack_zip_to(wheel_fname, 'wheel')
        package_paths = []
        for entry in os.listdir('wheel'):
            fname = pjoin('wheel', entry)
            if isdir(fname):
                if exists(pjoin(fname, '__init__.py')):
                    package_paths.append(fname)
        for package_path in package_paths:
            lib_path = pjoin(package_path, lib_sdir)
            if exists(lib_path):
                raise DelocationError(
                    '{0} already exists in wheel'.format(lib_path))
            delocate_path(package_path, lib_path,
                          lib_filt_func, copy_filt_func)
            if len(os.listdir(lib_path)) == 0:
                shutil.rmtree(lib_path)
        _pack_zip_to('wheel', wheel_fname)


def wheel_libs(wheel_fname, lib_filt_func = None):
    """ Collect unique install names from package(s) in wheel file

    Parameters
    ----------
    wheel_fname : str
        Filename of wheel
    lib_filt_func : None or callable, optional
        If None, inspect all files for install names. If callable, accepts
        filename as argument, returns True if we should inspect the file, False
        otherwise.

    Returns
    -------
    lib_dict : dict
        dictionary with (key, value) pairs of (install name, set of files in
        wheel packages with install name)
    """
    wheel_fname = abspath(wheel_fname)
    lib_dict = {}
    with InTemporaryDirectory():
        _unpack_zip_to(wheel_fname, 'wheel')
        package_paths = []
        for entry in os.listdir('wheel'):
            fname = pjoin('wheel', entry)
            if isdir(fname):
                if exists(pjoin(fname, '__init__.py')):
                    package_paths.append(fname)
        for package_path in package_paths:
            pkg_lib_dict = tree_libs(package_path, lib_filt_func)
            for key, values in pkg_lib_dict.items():
                if not key in lib_dict:
                    lib_dict[key] = values
                else:
                    lib_dict[key] += values
    return lib_dict
