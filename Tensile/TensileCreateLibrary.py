################################################################################
#
# Copyright (C) 2016-2024 Advanced Micro Devices, Inc. All rights reserved.
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
################################################################################

# This script only gets called by CMake

if __name__ == "__main__":
    print(
        "This file can no longer be run as a script.  Run 'Tensile/bin/TensileCreateLibrary' instead."
    )
    exit(1)

import collections
import itertools
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import warnings
from io import TextIOWrapper
from pathlib import Path
from typing import Any, Callable, Dict, Generator, List, Optional, Set, Tuple, Union

from Tensile.Tensile import LibraryLogic

from . import ClientExecutable, Common, EmbeddedData, LibraryIO, Utils
from .Common import (
    HR,
    CHeader,
    CMakeHeader,
    assignGlobalParameters,
    ensurePath,
    getArchitectureName,
    gfxName,
    globalParameters,
    printExit,
    printWarning,
    supportedCompiler,
    tPrint,
    which,
)
from .KernelWriterAssembly import KernelWriterAssembly
from .KernelWriterBase import KernelWriterBase
from .KernelWriterSource import KernelWriterSource
from .SolutionLibrary import MasterSolutionLibrary
from .SolutionStructs import Solution
from .TensileCreateLib.KernelFileContext import KernelFileContextManager
from .TensileCreateLib.ParseArguments import parseArguments
from .Utilities.Profile import profile
from .Utilities.String import splitDelimitedString
from .Utilities.toFile import toFile

TENSILE_MANIFEST_FILENAME = "TensileManifest.txt"
TENSILE_LIBRARY_DIR = "library"


################################################################################
def processKernelSource(kernel, kernelWriterSource, kernelWriterAssembly):
    """Generate source for a single kernel.
    Returns (error, source, header, kernelName).
    """
    try:
        kernelWriter = (
            kernelWriterSource if kernel["KernelLanguage"] == "Source" else kernelWriterAssembly
        )
        # get kernel name
        kernelName = kernelWriter.getKernelFileBase(kernel)
        (err, src) = kernelWriter.getSourceFileString(kernel)
        header = kernelWriter.getHeaderFileString(kernel)
        # will be put in Kernels.h/cpp if None
        filename = kernel.get("codeObjectFile", None)

    except RuntimeError:
        printWarning(
            "Gracefully handling unknown runtime error when generating kernel: %s"
            % kernel["KernelName"]
        )
        return (1, "", "", kernelName, None)

    return (err, src, header, kernelName, filename)


def getAssemblyCodeObjectFiles(kernels, kernelWriterAssembly, outputPath, removeTemporaries):
    destDir = ensurePath(os.path.join(outputPath, "library"))
    asmDir = kernelWriterAssembly.getAssemblyDirectory()
    assemblyKernels = list([k for k in kernels if k["KernelLanguage"] == "Assembly"])
    if len(assemblyKernels) == 0:
        return []

    archs = collections.defaultdict(list)
    for k in assemblyKernels:
        archs[tuple(k["ISA"])].append(k)
    coFiles = []
    for arch, archKernels in archs.items():
        archName = gfxName(arch)
        objectFiles = list(
            [
                kernelWriterAssembly.getKernelFileBase(k) + ".o"
                for k in archKernels
                if "codeObjectFile" not in k
            ]
        )

        numObjectFiles = len([1 for k in archKernels if k["KernelLanguage"] == "Assembly"])

        if numObjectFiles == 0:
            continue
        if (
            globalParameters["MergeFiles"]
            or globalParameters["NumMergedFiles"] > 1
            or globalParameters["LazyLibraryLoading"]
        ):

            # Group kernels from placeholder libraries
            coFileMap = collections.defaultdict(list)

            if len(objectFiles):
                coFileMap[os.path.join(destDir, "TensileLibrary_" + archName + ".co")] = objectFiles

            for kernel in archKernels:
                coName = kernel.get("codeObjectFile", None)
                if coName:
                    coFileMap[os.path.join(destDir, coName + ".co")] += [
                        kernelWriterAssembly.getKernelFileBase(kernel) + ".o"
                    ]

            for coFile, objectFiles in coFileMap.items():
                args = []
                if os.name == "nt":
                    # On Windows, the objectFiles list command line (including spaces)
                    # exceeds the limit of 8191 characters, so using response file

                    responseArgs = objectFiles
                    responseFile = os.path.join(asmDir, "clangArgs.txt")
                    with open(responseFile, "wt") as file:
                        file.write(" ".join(responseArgs))
                        file.flush()

                    args = kernelWriterAssembly.getLinkCodeObjectArgs(["@clangArgs.txt"], coFile)
                else:
                    args = kernelWriterAssembly.getLinkCodeObjectArgs(objectFiles, coFile)

                tPrint(2, "Linking objects into co files: " + " ".join(args))

                # change to use  check_output to force windows cmd block util command finish
                try:
                    out = subprocess.check_output(args, stderr=subprocess.STDOUT, cwd=asmDir)
                    tPrint(3, out)
                except subprocess.CalledProcessError as err:
                    print(err.output)
                    raise

                coFiles.append(coFile)
        else:
            # no mergefiles

            assemblyKernelNames = [kernelWriterAssembly.getKernelFileBase(k) for k in archKernels]
            origCOFiles = [os.path.join(asmDir, k + ".co") for k in assemblyKernelNames]
            newCOFiles = [
                os.path.join(destDir, k + "_" + archName + ".co") for k in assemblyKernelNames
            ]

            for src, dst in (
                zip(origCOFiles, newCOFiles)
                if globalParameters["PrintLevel"] == 0
                else Utils.tqdm(zip(origCOFiles, newCOFiles), desc="Relocating code objects")
            ):
                shutil.copyfile(src, dst)
            coFiles += newCOFiles

    return coFiles


def splitArchs():
    # Helper for architecture
    def isSupported(arch):
        return (
            globalParameters["AsmCaps"][arch]["SupportedISA"]
            and globalParameters["AsmCaps"][arch]["SupportedSource"]
        )

    if ";" in globalParameters["Architecture"]:
        wantedArchs = globalParameters["Architecture"].split(";")
    else:
        wantedArchs = globalParameters["Architecture"].split("_")
    archs = []
    cmdlineArchs = []

    if "all" in wantedArchs:
        for arch in globalParameters["SupportedISA"]:
            if isSupported(arch):
                if arch == (9, 0, 6) or arch == (9, 0, 8) or arch == (9, 0, 10):
                    if arch == (9, 0, 10):
                        archs += [gfxName(arch) + "-xnack+"]
                        cmdlineArchs += [gfxName(arch) + ":xnack+"]
                    archs += [gfxName(arch) + "-xnack-"]
                    cmdlineArchs += [gfxName(arch) + ":xnack-"]
                else:
                    archs += [gfxName(arch)]
                    cmdlineArchs += [gfxName(arch)]
    else:
        for arch in wantedArchs:
            archs += [re.sub(":", "-", arch)]
            cmdlineArchs += [arch]
    return archs, cmdlineArchs


def buildSourceCodeObjectFile(CxxCompiler, outputPath, kernelFile, removeTemporaries):
    buildPath = ensurePath(os.path.join(globalParameters["WorkingPath"], "code_object_tmp"))
    destDir = ensurePath(os.path.join(outputPath, "library"))
    (_, filename) = os.path.split(kernelFile)
    (base, _) = os.path.splitext(filename)

    if "CmakeCxxCompiler" in globalParameters and globalParameters["CmakeCxxCompiler"] is not None:
        os.environ["CMAKE_CXX_COMPILER"] = globalParameters["CmakeCxxCompiler"]

    objectFilename = base + ".o"
    soFilename = base + ".so"

    coFilenames = []

    if supportedCompiler(CxxCompiler):
        archs, cmdlineArchs = splitArchs()

        archFlags = ["--offload-arch=" + arch for arch in cmdlineArchs]

        # needs to be fixed when Maneesh's change is made available
        hipFlags = ["-D__HIP_HCC_COMPAT_MODE__=1"]
        hipFlags += (
            ["--genco"] if CxxCompiler == "hipcc" else ["--cuda-device-only", "-x", "hip", "-O3"]
        )
        # if CxxCompiler == "amdclang++":
        # hipFlags += ["-mllvm", "-amdgpu-early-inline-all=true", "-mllvm", "-amdgpu-function-calls=false"]
        hipFlags += ["-I", outputPath]

        # Add build-id for builds with rocm 5.3+
        compilerVer = globalParameters["HipClangVersion"].split(".")[:2]
        compilerVer = [int(c) for c in compilerVer]
        if len(compilerVer) >= 2 and (
            compilerVer[0] > 5 or (compilerVer[0] == 5 and compilerVer[1] > 2)
        ):
            hipFlags += ["-Xoffload-linker", "--build-id"]

        launcher = shlex.split(os.environ.get("Tensile_CXX_COMPILER_LAUNCHER", ""))

        if os.name == "nt":
            hipFlags += [
                "-std=c++14",
                "-fms-extensions",
                "-fms-compatibility",
                "-fPIC",
                "-Wno-deprecated-declarations",
            ]
            compileArgs = (
                launcher
                + [which(CxxCompiler)]
                + hipFlags
                + archFlags
                + [kernelFile, "-c", "-o", os.path.join(buildPath, objectFilename)]
            )
        else:
            compileArgs = (
                launcher
                + [which(CxxCompiler)]
                + hipFlags
                + archFlags
                + [kernelFile, "-c", "-o", os.path.join(buildPath, objectFilename)]
            )

        tPrint(2, f"Build object file command: {compileArgs}")
        # change to use  check_output to force windows cmd block util command finish
        try:
            out = subprocess.check_output(compileArgs, stderr=subprocess.STDOUT)
            tPrint(3, out)
        except subprocess.CalledProcessError as err:
            print(err.output)
            raise

        # get hipcc version due to compatiblity reasons
        # If we aren't using hipcc what happens?
        hipccver = globalParameters["HipClangVersion"].split(".")
        hipccMaj = int(hipccver[0])
        hipccMin = int(hipccver[1])

        # for hipclang 5.2 and above, clang offload bundler changes the way input/output files are specified
        inflag = "-inputs"
        outflag = "-outputs"
        if (hipccMaj == 5 and hipccMin >= 2) or hipccMaj >= 6:
            inflag = "-input"
            outflag = "-output"

        infile = os.path.join(buildPath, objectFilename)
        bundler = globalParameters["ClangOffloadBundlerPath"]
        if bundler is None:
            raise ValueError(
                "No bundler available; set TENSILE_ROCM_OFFLOAD_BUNDLER_PATH to point to clang-offload-bundler."
            )
        try:
            bundlerArgs = [bundler, "-type=o", "%s=%s" % (inflag, infile), "-list"]
            listing = (
                subprocess.check_output(bundlerArgs, stderr=subprocess.STDOUT).decode().split("\n")
            )
            for target in listing:
                matched = re.search("gfx.*$", target)
                if matched:
                    arch = re.sub(":", "-", matched.group())
                    if "TensileLibrary" in base and "fallback" in base:
                        outfile = os.path.join(buildPath, "{0}_{1}.hsaco".format(base, arch))
                    elif "TensileLibrary" in base:
                        variant = [t for t in ["", "xnack-", "xnack+"] if t in target][-1]
                        baseVariant = base + "-" + variant if variant else base
                        if arch in baseVariant:
                            outfile = os.path.join(buildPath, baseVariant + ".hsaco")
                        else:
                            outfile = None
                    else:
                        outfile = os.path.join(
                            buildPath, "{0}-000-{1}.hsaco".format(soFilename, arch)
                        )

                    # Compilation
                    if outfile:
                        coFilenames.append(os.path.split(outfile)[1])
                        # bundlerArgs = [bundler, "-type=o", "-targets=%s" % target, "-inputs=%s" % infile, "-outputs=%s" % outfile, "-unbundle"]
                        bundlerArgs = [
                            bundler,
                            "-type=o",
                            "-targets=%s" % target,
                            "%s=%s" % (inflag, infile),
                            "%s=%s" % (outflag, outfile),
                            "-unbundle",
                        ]
                        tPrint(2, "Build source code object file: " + " ".join(bundlerArgs))
                        # change to use  check_output to force windows cmd block util command finish
                        out = subprocess.check_output(bundlerArgs, stderr=subprocess.STDOUT)
                        tPrint(3, out)

        except subprocess.CalledProcessError as err:
            tPrint(1, err.output)
            for i in range(len(archs)):
                outfile = os.path.join(buildPath, "{0}-000-{1}.hsaco".format(soFilename, archs[i]))
                coFilenames.append(os.path.split(outfile)[1])
                # bundlerArgs = [bundler, "-type=o", "-targets=hip-amdgcn-amd-amdhsa--%s" % cmdlineArchs[i], "-inputs=%s" % infile, "-outputs=%s" % outfile, "-unbundle"]
                bundlerArgs = [
                    bundler,
                    "-type=o",
                    "-targets=hip-amdgcn-amd-amdhsa--%s" % cmdlineArchs[i],
                    "%s=%s" % (inflag, infile),
                    "%s=%s" % (outflag, outfile),
                    "-unbundle",
                ]
                tPrint(2, "Build source code object file: " + " ".join(bundlerArgs))
                # change to use  check_output to force windows cmd block util command finish
                try:
                    out = subprocess.check_output(bundlerArgs, stderr=subprocess.STDOUT)
                    tPrint(3, out)
                except subprocess.CalledProcessError as err:
                    tPrint(1, err.output)
                    raise

    else:
        raise RuntimeError("Unknown compiler {}".format(CxxCompiler))

    coFilenames = [name for name in coFilenames]
    extractedCOs = [os.path.join(buildPath, name) for name in coFilenames]
    destCOsList = [os.path.join(destDir, name) for name in coFilenames]
    for src, dst in zip(extractedCOs, destCOsList):
        if removeTemporaries:
            shutil.move(src, dst)
        else:
            shutil.copyfile(src, dst)

    return destCOsList


def buildSourceCodeObjectFiles(CxxCompiler, kernelFiles, outputPath, removeTemporaries):
    args = zip(
        itertools.repeat(CxxCompiler),
        itertools.repeat(outputPath),
        kernelFiles,
        itertools.repeat(removeTemporaries),
    )
    coFiles = Common.ParallelMap(buildSourceCodeObjectFile, args, "Compiling source kernels")

    return itertools.chain.from_iterable(coFiles)


################################################################################
def prepAsm(
    kernelWriterAssembly: KernelWriterAssembly,
    isLinux: bool,
    buildPath: Path,
    isa: Tuple[int, int, int],
    printLevel: int,
):
    """Create and prepare the assembly directory; called ONCE per output directory.

    This function is called once per output directory. It creates a directory
    "assembly" under the provided **buildPath**, and generates a bash script for
    compiling object files into code object files.

    Args:
        kernelWriterAssembly: Assembly writer object.
        buildPath: Path to directory where assembly files will be written.
    """
    asmPath = buildPath / "assembly"
    asmPath.mkdir(exist_ok=True)

    assemblerFileName = asmPath / f"asm-new.{'sh' if isLinux else 'bat'}"

    with open(assemblerFileName, "w") as assemblerFile:
        if isLinux:
            assemblerFile.write("#!/bin/sh {log}\n".format(log="-x" if printLevel >= 3 else ""))
            assemblerFile.write("# usage: asm-new.sh kernelName(no extension) [--wave32]\n")

            assemblerFile.write("f=$1\n")
            assemblerFile.write("shift\n")
            assemblerFile.write('if [ ! -z "$1" ] && [ "$1" = "--wave32" ]; then\n')
            assemblerFile.write("    wave=32\n")
            assemblerFile.write("    shift\n")
            assemblerFile.write("else\n")
            assemblerFile.write("    wave=64\n")
            assemblerFile.write("fi\n")

            assemblerFile.write("h={gfxName}\n".format(gfxName=Common.gfxName(isa)))

            cArgs32 = kernelWriterAssembly.getCompileArgs("$f.s", "$f.o", isa=isa, wavefrontSize=32)
            cArgs64 = kernelWriterAssembly.getCompileArgs("$f.s", "$f.o", isa=isa, wavefrontSize=64)
            lArgs = kernelWriterAssembly.getLinkCodeObjectArgs(["$f.o"], "$f.co")

            assemblerFile.write("if [ $wave -eq 32 ]; then\n")
            assemblerFile.write(" ".join(cArgs32) + "\n")
            assemblerFile.write("else\n")
            assemblerFile.write(" ".join(cArgs64) + "\n")
            assemblerFile.write("fi\n")

            assemblerFile.write(" ".join(lArgs) + "\n")

            assemblerFile.write("cp $f.co ../../../library/${f}_$h.co\n")
            assemblerFile.write("mkdir -p ../../../asm_backup && ")
            assemblerFile.write("cp $f.s ../../../asm_backup/$f.s\n")
        else:
            assemblerFile.write("@echo off\n")
            assemblerFile.write("set f=%1\n\n")
            assemblerFile.write("set arg2=--wave64\n")
            assemblerFile.write("if [%2] NEQ [] set arg2=%2\n\n")
            assemblerFile.write("set /A wave=64\n")
            assemblerFile.write("if %arg2% EQU --wave32 set /A wave=32\n\n")

            assemblerFile.write("set h={gfxName}\n".format(gfxName=Common.gfxName(isa)))

            cArgs32 = " ".join(
                kernelWriterAssembly.getCompileArgs("%f%.s", "%f%.o", isa=isa, wavefrontSize=32)
            )
            cArgs64 = " ".join(
                kernelWriterAssembly.getCompileArgs("%f%.s", "%f%.o", isa=isa, wavefrontSize=64)
            )
            lArgs = " ".join(kernelWriterAssembly.getLinkCodeObjectArgs(["%f%.o"], "%f%.co"))

            assemblerFile.write(f"if %wave% == 32 ({cArgs32}) else ({cArgs64})\n")
            assemblerFile.write(f"{lArgs}\n")
            assemblerFile.write("copy %f%.co ..\..\..\library\%f%_%h%.co\n")
    os.chmod(assemblerFileName, 0o777)


################################################################################
def buildKernelSourceAndHeaderFiles(results, outputPath):
    """
    Logs errors and writes appropriate info to kernelSourceFile and kernelHeaderFile.

    Arguments:
      results:              list of (err, src, header, kernelName, filename)
      outputPath:           path to source directory
      kernelsWithBuildErrs: Dictionary to be updated with kernels that have errors
      kernelSourceFile:     File to write source data to
      kernelHeaderFile:     File to write header data to

    Returns:
      sourceFilenames:      Array containing source kernel filenames
    """

    # Find kernels to write
    kernelsToWrite = []
    kernelsWithBuildErrs: Dict[str, int] = {}
    filesToWrite = collections.defaultdict(list)
    validKernelCount = 0
    for err, src, header, kernelName, filename in results:

        # Keep track of kernels with errors
        if err:
            kernelsWithBuildErrs[kernelName] = err

        # Don't create a file for empty kernels
        if len(src.strip()) == 0:
            continue

        kernelsToWrite.append((err, src, header, kernelName))

        # Create list of files
        if filename:
            filesToWrite[os.path.join(os.path.normcase(outputPath), filename)].append(
                (err, src, header, kernelName)
            )
        elif globalParameters["MergeFiles"]:
            kernelSuffix = ""
            if globalParameters["NumMergedFiles"] > 1:
                kernelSuffix = validKernelCount % globalParameters["NumMergedFiles"]

            filesToWrite[
                os.path.join(os.path.normcase(outputPath), "Kernels" + kernelSuffix)
            ].append((err, src, header, kernelName))
        else:
            filesToWrite[os.path.join(os.path.normcase(outputPath), kernelName)].append(
                (err, src, header, kernelName)
            )
        validKernelCount += 1

    # Ensure there's at least one kernel file for helper kernels
    if globalParameters["LazyLibraryLoading"] or (
        globalParameters["MergeFiles"] and not kernelsToWrite
    ):
        kernelSuffix = ""
        if globalParameters["NumMergedFiles"] > 1:
            kernelSuffix = "0"

        filesToWrite[os.path.join(os.path.normcase(outputPath), "Kernels" + kernelSuffix)] = []

    # Write kernel data to files
    # Parse list of files and write kernels
    for filename, kernelList in filesToWrite.items():
        with open(filename + ".h", "w", encoding="utf-8") as kernelHeaderFile, open(
            filename + ".cpp", "w", encoding="utf-8"
        ) as kernelSourceFile:

            kernelSourceFile.write(CHeader)
            kernelHeaderFile.write(CHeader)
            kernelSourceFile.write('#include "{}.h"\n'.format(filename))
            kernelHeaderFile.write("#pragma once\n")
            if globalParameters["RuntimeLanguage"] == "HIP":
                kernelHeaderFile.write("#include <hip/hip_runtime.h>\n")
                kernelHeaderFile.write("#include <hip/hip_ext.h>\n\n")
            kernelHeaderFile.write('#include "KernelHeader.h"\n\n')

            for err, src, header, kernelName in kernelList:
                kernelSourceFile.write(src)
                kernelHeaderFile.write(header)

    sourceFilenames = [filePrefix + ".cpp" for filePrefix in filesToWrite]

    return sourceFilenames, kernelsWithBuildErrs


def markDuplicateKernels(
    kernels: List[Solution], kernelWriterAssembly: KernelWriterAssembly
) -> List[Solution]:
    """Marks duplicate assembly kernels based on their generated base file names.

    Kernels written in Assembly language may generate duplicate output file names,
    leading to potential race conditions. This function identifies such duplicates within
    the provided list of Solution objects and marks them to prevent issues.

    Args:
        kernels: A list of Solution objects representing kernels to be processed.

    Returns:
        A modified list of Solution objects where kernels identified as duplicates
        are marked with a `duplicate` attribute indicating their duplication status.

    Notes:
        This function sets the "duplicate" attribute on Solution objects, and thereby prepares
        kernels for **processKernelSource**, which requires "duplicate" to be set.
    """
    # Kernels may be intended for different .co files, but generate the same .o file
    # Mark duplicate kernels to avoid race condition
    # @TODO improve organization so this problem doesn't appear
    visited = set()
    count = 0
    for kernel in kernels:
        if kernel["KernelLanguage"] == "Assembly":
            curr = kernelWriterAssembly.getKernelFileBase(kernel)
            kernel.duplicate = curr in visited
            count += kernel.duplicate
            visited.add(curr)
    if count:
        printWarning(f"Found {count} duplicate kernels, these will be ignored")
    return kernels


def filterProcessingErrors(
    kernels: List[Solution],
    results: List[Any],
    printLevel: int,
    errorTolerant: bool,
) -> Tuple[List[Solution], List[Solution], List[Any]]:
    """Filters out processing errors from lists of kernels, solutions, and results.

    This function iterates through the results of **processKernelSource** and identifies
    any errors encountered during processing. If an error is found (-2 error code),
    the corresponding kernel, solution, and result are appended to separate lists
    for removal. After processing, items identified for removal are deleted from the
    original lists of kernels, solutions, and results.

    Args:
        kernels: List of Solution objects representing kernels.
        solutions: List of Solution objects associated with kernels.
        results: List of tuples representing processing results.
        printLevel: Print level indicator.

    Returns:
        Tuple[List[Solution], List[Solution], List[Any]]: Tuple containing filtered lists
        of kernels, solutions, and results after removing items with processing errors.

    Raises:
        KeyError: If 'PrintLevel' key is not found in the params dictionary.
    """
    removeKernels = []
    removeResults = []
    for kernIdx, res in (
        enumerate(results)
        if globalParameters["PrintLevel"] == 0
        else Utils.tqdm(enumerate(results), desc="Filtering processing errors")
    ):
        (err, src, header, kernelName, filename) = res
        if err == -2:
            if not errorTolerant:
                print(
                    "\nKernel generation failed for kernel: {}".format(
                        kernels[kernIdx]["SolutionIndex"]
                    )
                )
                print(kernels[kernIdx]["SolutionNameMin"])
            removeKernels.append(kernels[kernIdx])
            removeResults.append(results[kernIdx])
    if len(removeKernels) > 0 and not errorTolerant:
        printExit("** kernel generation failure **")
    for kern in removeKernels:
        kernels.remove(kern)
    for rel in removeResults:
        results.remove(rel)


def filterBuildErrors(
    kernels: List[Solution],
    kernelsWithBuildErrors: Dict[str, int],
    writerSelectionFn: Callable[[str], Union[KernelWriterSource, KernelWriterAssembly]],
    ignoreErr: bool,
) -> List[Solution]:
    """Filters a list of kernels based on build errors and error tolerance.

    Args:
        kernels: A list of `Solution` objects representing kernels to filter.
        kernelsWithBuildErrors: A list of `Solution` objects that have build errors.
        errorTolerant: A boolean indicating whether to tolerate build errors.

    Returns:
        A filtered list of kernels (**Solution** objects) that are eligible for building.

    Raises:
        SystemExit: If **ignoreErr** is False and any kernels have build errors.
    """
    if not ignoreErr and len(kernelsWithBuildErrors) > 0:
        raise RuntimeError(
            "Kernel compilation failed in one or more subprocesses. "
            "Consider setting CpuThreads=0 and re-run to debug."
        )

    def noBuildError(kernel):
        kernelName = writerSelectionFn(kernel["KernelLanguage"]).getKernelName(kernel)
        return kernelName not in kernelsWithBuildErrors

    return list(filter(noBuildError, kernels))


def getKernelSourceAndHeaderCode(ko: KernelWriterBase) -> Tuple[int, List[str], List[str], str]:
    """Get the source and header content for a kernel object.

    Arguments:
        ko: Kernel object to extract content from.

    Returns:
        Tuple of data: (error code, source code, header code, kernel name)
    """
    name = ko.getKernelName()
    err, src = ko.getSourceFileString()
    hdr = ko.getHeaderFileString()
    return err, [CHeader, src], [CHeader, hdr], name


def writeKernelHelpers(
    kernelHelperObj: KernelWriterBase,
    kernelSourceFile: Optional[TextIOWrapper],
    kernelHeaderFile: Optional[TextIOWrapper],
    outputPath: Path,
    kernelFiles: List[str],
):
    """Writes the source and header code generated by a kernel helper object to specified files or a new file.

    Args:
        kernelHelperObj: The kernel helper object providing source and header code.
        kernelSourceFile: The file object for the kernel's source. If None, a new file is created.
        kernelHeaderFile: The file object for the kernel's header. If None, a new file is created.
        outputPath: The directory path where new files should be saved if `kernelSourceFile` and
            `kernelHeaderFile` are None.
        kernelFiles: A list of kernel file names to be updated with the new kernel name if new
            files are created.

    Notes:
        - If `kernelSourceFile` and `kernelHeaderFile` are provided, the source and header code
          are appended to these files.
        - If these file objects are None, new `.cpp` and `.h` files are created in the
          `outputPath/Kernels` directory named after the kernel.
        - The function appends the new kernel name to `kernelFiles` if new files are created.
    """
    err, srcCode, hdrCode, kernelName = getKernelSourceAndHeaderCode(kernelHelperObj)
    if err:
        printWarning(f"Invalid kernel: {kernelName} may be corrupt")
    if kernelSourceFile and kernelHeaderFile:  # Append to existing files => mergeFiles == True
        toFile(kernelSourceFile, srcCode)
        toFile(kernelHeaderFile, hdrCode)
    else:  # Write to new a file for each helper => mergeFiles == False. Default behaviour when called through rocBLAS
        srcFilename = Path(outputPath) / "Kernels" / f"{kernelName}.cpp"
        hdrFilename = Path(outputPath) / "Kernels" / f"{kernelName}.h"
        toFile(srcFilename, srcCode)
        toFile(hdrFilename, hdrCode)
        kernelFiles.append(str(srcFilename))


################################################################################
# Write Solutions and Kernels for BenchmarkClient or LibraryClient
################################################################################
def writeKernels(
    outputPath: str,
    cxxCompiler: str,
    params: Dict[str, Any],
    kernels: List[Solution],
    kernelHelperObjs: List[KernelWriterBase],
    kernelWriterSource: KernelWriterSource,
    kernelWriterAssembly: KernelWriterAssembly,
    errorTolerant: bool = False,
    removeTemporaries: bool = True,
):
    start = time.time()

    # Push working path into build_tmp folder because there may be more than
    # one process running this script. This is to avoid build directory clashing.
    # NOTE: file paths must not contain the lower case word 'kernel' or the
    # /opt/rocm/bin/extractkernel will fail.
    # See buildSourceCodeObjectFile:167 for the call to this binary.

    ## TODO: Is there a way to get this to work without changing global state?
    Common.pushWorkingPath("build_tmp")
    Common.pushWorkingPath(os.path.basename(outputPath).upper())

    tPrint(1, "# Writing Kernels...")

    ## TODO: This may be unused
    if not params["MergeFiles"] or params["NumMergedFiles"] > 1 or params["LazyLibraryLoading"]:
        ensurePath(os.path.join(outputPath, "Kernels"))

    ## This uses global state from "WorkingPath"
    prepAsm(
        kernelWriterAssembly,
        os.name != "nt",
        # Use globalParameters here, not params
        Path(globalParameters["WorkingPath"]),
        globalParameters["CurrentISA"],
        params["PrintLevel"],
    )

    kernels = markDuplicateKernels(kernels, kernelWriterAssembly)

    kIter = zip(
        kernels,
        itertools.repeat(kernelWriterSource),
        itertools.repeat(kernelWriterAssembly),
    )
    results = Common.ParallelMap(processKernelSource, list(kIter), "Generating kernels")


    filterProcessingErrors(kernels, results, params["PrintLevel"], errorTolerant)

    kernelFiles, kernelsWithBuildErrors = buildKernelSourceAndHeaderFiles(results, outputPath)

    writerSelector = lambda lang: kernelWriterAssembly if lang == "Assembly" else kernelWriterSource
    kernelsToBuild = filterBuildErrors(
        kernels, kernelsWithBuildErrors, writerSelector, errorTolerant
    )

    outPath = Path(outputPath)
    with KernelFileContextManager(
        params["LazyLibraryLoading"],
        params["MergeFiles"],
        params["NumMergedFiles"],
        outPath,
        kernelFiles,
    ) as (srcFile, hdrFile):
        for ko in kernelHelperObjs:
            writeKernelHelpers(ko, srcFile, hdrFile, outPath, kernelFiles)

    codeObjectFiles = []
    if not globalParameters["GenerateSourcesAndExit"]:
        codeObjectFiles += buildSourceCodeObjectFiles(
            cxxCompiler, kernelFiles, outputPath, removeTemporaries
        )
        codeObjectFiles += getAssemblyCodeObjectFiles(
            kernelsToBuild,
            kernelWriterAssembly,
            outputPath,
            removeTemporaries,
        )

    stop = time.time()
    tPrint(1, "# Kernel Building elapsed time = %.1f secs" % (stop - start))

    Common.popWorkingPath()  # outputPath.upper()
    Common.popWorkingPath()  # build_tmp


##############################################################################
# Min Naming / Solution and Kernel Writers
##############################################################################
def getKernelWriters(kernels: List[Solution], removeTemporaries):

    # if any kernels are assembly, append every ISA supported
    kernelSerialNaming = Solution.getSerialNaming(kernels)

    kernelMinNaming = Solution.getMinNaming(kernels)
    kernelWriterSource = KernelWriterSource(kernelMinNaming, kernelSerialNaming, removeTemporaries)
    kernelWriterAssembly = KernelWriterAssembly(
        kernelMinNaming, kernelSerialNaming, removeTemporaries
    )

    return (kernelWriterSource, kernelWriterAssembly)


################################################################################
# copy static cpp files and headers
################################################################################
def copyStaticFiles(outputPath=None):
    if outputPath is None:
        outputPath = globalParameters["WorkingPath"]
    libraryStaticFiles = [
        "TensileTypes.h",
        "tensile_bfloat16.h",
        "tensile_float8_bfloat8.h",
        "hip_f8_impl.h",
        "KernelHeader.h",
    ]

    for fileName in libraryStaticFiles:
        # copy file
        shutil.copy(os.path.join(globalParameters["SourcePath"], fileName), outputPath)

    return libraryStaticFiles


################################################################################
# Generate Kernel Objects From Solutions
################################################################################
def generateKernelObjectsFromSolutions(kernels: List[Solution])
    return (k.getHelperKernelObjects() for k in kernels)


def addNewLibrary(
    masterLibraries: Dict[str, MasterSolutionLibrary],
    newLibrary: MasterSolutionLibrary,
    architectureName: str,
) -> int:
    """Adds new master solution library to a master solution libraries dict.

    For a given architecture, add the new library to a dictionary containing
    libraries for all architectures, compute the starting index for the new
    library, then remap the indexes for all of the solutions associated with
    the library.

    Args:
        masterLibraries: A dictionary containing all master solution libraries for all architectures.
        newLibrary: A master solution library to add to the dictionary.
        architectureName: The name of the architecture (or key) associated with the library.

    Returns:
        Index to the last solution of the library associated with current architecture.
    """
    masterLibraries[architectureName] = newLibrary
    archIndex = MasterSolutionLibrary.ArchitectureIndexMap(architectureName)
    masterLibraries[architectureName].remapSolutionIndicesStartingFrom(archIndex)
    return archIndex


def makeMasterLibraries(
    logicList: List[LibraryIO.LibraryLogic], separate: bool
) -> Dict[str, MasterSolutionLibrary]:
    """Creates a dictionary of master solution libraries.

    Iterates through a list of LibraryLogic objects creating
    master solution libraries and modifying the solution
    indexing as required.

    Args:
        logicFiles: List of LibraryLogic objects.
        separate: Separate libraries by architecture.

    Returns:
        An architecture separated master solution libraries
        or a single master solution library for all architectures.
    """
    masterLibraries = {}
    nextSolIndex = {}
    fullMasterLibrary = None

    for logic in logicList:
        (_, architectureName, _, solutionsForSchedule, _, newLibrary) = logic
        if separate:
            if architectureName in masterLibraries:
                nextSolIndex[architectureName] = masterLibraries[architectureName].merge(
                    newLibrary, nextSolIndex[architectureName]
                )
            else:
                nextSolIndex[architectureName] = addNewLibrary(
                    masterLibraries, newLibrary, architectureName
                )
        else:
            if fullMasterLibrary:
                fullMasterLibrary.merge(newLibrary)
            else:
                fullMasterLibrary = newLibrary

    return {"full": fullMasterLibrary} if fullMasterLibrary is not None else masterLibraries


def addFallback(masterLibraries: Dict[str, MasterSolutionLibrary]) -> None:
    """Adds fallback library.

    Given a master solution library, add a fallback and if the corresponding
    architecture is unsupported, replace the library altogether with a fallback.

    Args:
        masterLibraries: A dictionary containing the master solution libraries.
    """
    archs, _ = splitArchs()

    for key, value in masterLibraries.items():
        if key != "fallback":
            value.insert(masterLibraries["fallback"])

    for archName in archs:
        archName = archName.split("-", 1)[0]
        if archName not in masterLibraries:
            tPrint(1, "Using fallback for arch: " + archName)
            masterLibraries[archName] = masterLibraries["fallback"]

    masterLibraries.pop("fallback")


def applyNaming(masterLibraries: Dict[str, MasterSolutionLibrary]) -> None:
    """Assigns the solution code object file name for lazy libraries.

    Given a master solution library with lazy libraries, assigns the
    key associated with the lazy library (or name) as the value
    assiciated with the corresponding solution's code object file.

    Args:
        masterLibraries: A dictionary containing the master solution libraries.
    """
    for masterLibrary in masterLibraries.values():
        for name, lib in masterLibrary.lazyLibraries.items():
            for sol in lib.solutions.values():
                sol.originalSolution["codeObjectFile"] = name


def makeSolutions(
    masterLibraries: dict, separate: bool
):  # -> Generator[Solution]:# is breaking tensile
    """Extracts the solutions from the master solution library.

    Given a master solution library, forms a flattened generator that
    yields solutions by iterating over all of the solutions contained
    in the master solution libraries. If using separate architectures
    but not using lazy loading, lazyLibraries should be an empty dict.

    Args:
        masterLibraries: A dictionary containing the master solution libraries.

    Returns:
        Generator representing a sequence of library logic tuples.
    """
    gen1 = (
        sol.originalSolution
        for masterLibrary in masterLibraries.values()
        for sol in masterLibrary.solutions.values()
    )
    gen2 = (
        sol.originalSolution
        for masterLibrary in masterLibraries.values()
        for lib in masterLibrary.lazyLibraries.values()
        for sol in lib.solutions.values()
    )
    return itertools.chain(gen1, gen2)


def parseLibraryLogicFiles(logicFiles: List[str]) -> List[LibraryIO.LibraryLogic]:
    """Load and parse logic (yaml) files.

    Given a list of paths to yaml files containing library logic, load the files
    into memory and parse the data into a named tuple (i.e. LibraryLogic). This
    operation is parallelized over N processes.

    Args:
        logicFiles: List of paths to logic files.

    Returns:
        List of library logic tuples.
    """
    return Common.ParallelMap(
        LibraryIO.parseLibraryLogicFile, logicFiles, "Reading logic files", multiArg=False
    )


def makeMasterLibraries2(
    logicFiles: List[LibraryIO.LibraryLogic], version: str, separate: bool
) -> Dict[str, MasterSolutionLibrary]:
    """Generates a dictionary of master solution libraries.

    Args:
        logicFiles: List of paths to logic files.
        version: User provided version for the library.
        printLevel: Level of debug printing requested.
        separate: Separate libraries by architecture.

    Returns:
        For separate architectures, a dictionary of architecture
        separated master solution libraries; otherwise, a single
        master solution library for all architectures.
    """
    masterLibraries = makeMasterLibraries(logicFiles, separate)
    if separate and "fallback" in masterLibraries:
        addFallback(masterLibraries)
    applyNaming(masterLibraries)
    for lib in masterLibraries.values():
        lib.version = version

    return masterLibraries


def generateSolutions(libraryLogics: List[LibraryIO.LibraryLogic]) -> Generator[Solution]:
    """Generates a list of solutions.

    Args:
        masterLibraries: A dictionary of master solutions libraries.
        separate: Separate libraries by architecture.

    Returns:
        A solution list.
    """
    return (l for ll in libraryLogics for l in ll.solutions)


def findLogicFiles(
    path: Path,
    logicArchs: Set[str],
    extraMatchers: Set[str] = {"hip"},
) -> List[str]:
    """Recursively searches the provided path for logic files.

    Args:
        path: The path to the directory to search.
        logicArchs: Target logic archiectures. These are interepreted as filename substrings
            for which logic files are to be included.
        extraMatchers: Additional directories to include for logic files.

    Returns:
        A list of Path objects representing the found YAML files.
    """
    isMatch = lambda file: any((arch in file.stem for arch in logicArchs.union(extraMatchers)))
    isExperimental = lambda path: not experimentalDir in str(path)
    extensions = ["*.yaml", "*.yml"]
    logicFiles = filter(isMatch, (file for ext in extensions for file in path.rglob(ext)))

    return list(str(l) for l in logicFiles)





################################################################################
# Tensile Create Library
################################################################################
@profile
def TensileCreateLibrary():

    args = parseArguments()

    lazyLoading = args["LazyLibraryLoading"]
    separateArchs = args["SeparateArchitectures"]
    mergeFiles = args["MergeFiles"]
    cxxCompiler = args["CxxCompiler"]
    libraryFormat = args["LibraryFormat"]
    logicPath = args["LogicPath"]
    outputPath = args["OutputPath"]
    removeTemporaries = not args["KeepBuildTmp"]

    globalParameters["PrintLevel"] = args["PrintLevel"]

    ensurePath(outputPath)
    outputPath = os.path.abspath(outputPath)
    copyStaticFiles(outputPath)

    assignGlobalParameters(args)

    if not os.path.exists(logicPath):
        printExit("LogicPath %s doesn't exist" % logicPath)

    logicArchs = splitDelimitedString(args["Architecture"], {";", "_"})
    logicArchs = {name for name in (getArchitectureName(gfxName) for gfxName in logicArchs) if name}

    logicFiles = findLogicFiles(Path(logicPath), logicArchs)
    libraryLogics = parseLibraryLogicFiles(logicFiles)
    solns = generateSolutions(libraryLogics)                                                      # Mutates kernels internally
    kernels = (s.getKernels() for s in solns)
    kernelHelperObjs = generateKernelObjectsFromSolutions(kernels)
    kernelWriterSource, kernelWriterAssembly = getKernelWriters(kernels, removeTemporaries)

    writeKernels(
        outputPath,
        cxxCompiler,
        args,
        kernels,
        kernelHelperObjs,
        kernelWriterSource,
        kernelWriterAssembly,
        removeTemporaries=removeTemporaries,
    )

    newLibraryDir = Path(outputPath) / "library"
    newLibraryDir.mkdir(exist_ok=True)

    if removeTemporaries:
        buildTmp = Path(outputPath).parent / "build_tmp"
        if buildTmp.exists() and buildTmp.is_dir():
            shutil.rmtree(buildTmp)
