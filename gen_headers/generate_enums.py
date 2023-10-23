#!/usr/bin/env python3

#
# Freeciv - Copyright (C) 2023
#   This program is free software; you can redistribute it and/or modify
#   it under the terms of the GNU General Public License as published by
#   the Free Software Foundation; either version 2, or (at your option)
#   any later version.
#
#   This program is distributed in the hope that it will be useful,
#   but WITHOUT ANY WARRANTY; without even the implied warranty of
#   MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#   GNU General Public License for more details.
#

# This script runs under Python 3.6 and up. Please leave it so.
# It might also run under older versions, but no such guarantees are made.

"""generate_enums.py - code generation script for Freeciv specenums

This script takes enum definition files and turns them into a C header
defining those enums using specenum_gen.h, which is in turn generated by
utility/generate_specenum.py - the plan is to eventually, once all current
uses of specenum_gen.h have been migrated to this script, fold both scripts
into one. If you're reading this years into the future, it's hopefully
because you're looking through the version history, and *not* because this
convoluted system is still in place.

See specenum_gen.h or generate_specenum.py for what exactly the various
options mean.

# Definition file syntax

Like packets.def, the enum defs can use
# Python-style EOL comments,
// C++-style EOL comments,
/* and C-style comments,
 * which can span multiple lines. */

Each definition file consists of zero or more enum definitions, which take
the following form, where <angle brackets> are placeholders:

enum <SPECENUM_NAME>
  <enum options>
values
  <SPECENUM_VALUE0> <SPECENUM_VALUE0NAME (optional)>
  <SPECENUM_VALUE1> <SPECENUM_VALUE1NAME (optional)>
# ...
end

The following <enum options> are supported:
- prefix <prefix>
  prepended to all VALUEs, including ZERO and COUNT.
  Should include any desired final separator.
- generic <amount> <identifier>
  Generate <amount> values with no name and the given identifier
  followed by their index (starting at 1)
- bitwise
- zero <SPECENUM_ZERO> <SPECENUM_ZERONAME (optional)>
  (requires bitwise)
  If 'prefix' is also given, the <SPECENUM_ZERO> identifier is optional
  and defaults to ZERO
- count <SPECENUM_COUNT> <SPECENUM_COUNTNAME (optional)>
  (cannot be used with bitwise)
  If 'prefix' is also given, the <SPECENUM_COUNT> identifier is optional
  and defaults to COUNT
- invalid <SPECENUM_INVALID>
- name-override
- name-updater
- bitvector <SPECENUM_BITVECTOR>
  (cannot be used with bitwise)
"""


import re
import argparse
import sys
from pathlib import Path
from contextlib import contextmanager, ExitStack
from itertools import takewhile, zip_longest

import typing


###################### Parsing Command Line Arguments ######################

def file_path(s: "str | Path") -> Path:
    """Parse the given path and check basic validity."""
    path = Path(s)

    if path.is_reserved() or not path.name:
        raise ValueError(f"not a valid file path: {s!r}")
    if path.exists() and not path.is_file():
        raise ValueError(f"not a file: {s!r}")

    return path


class ScriptConfig:
    """Contains configuration info for the script's execution, along with
    functions closely tied to that configuration"""

    def_paths: "list[Path]"
    """Paths to definition files, in load order"""
    out_path: Path
    """Path to the output file"""

    verbose: bool
    """Whether to enable verbose logging"""
    lazy_overwrite: bool
    """Whether to lazily overwrite output files"""

    @staticmethod
    def get_argparser() -> argparse.ArgumentParser:
        """Construct an argument parser for a packet generation script"""
        parser = argparse.ArgumentParser(
            description = "Generate specenum header files",
            add_help = False,   # we'll add a help option explicitly
        )

        # Argument groups
        # Note the order:
        # We want the path arguments to show up *first* in the help text

        paths = parser.add_argument_group(
            "Input and output paths",
            "The following parameters decide which files to read and write.",
        )

        script = parser.add_argument_group(
            "Script configuration",
            "The following parameters change how the script operates.",
        )

        # Individual arguments
        # Note the order:
        # We want the path arguments to show up *last* in the usage summary

        script.add_argument("-h", "--help", action = "help",
                            help = "show this help message and exit")

        script.add_argument("-v", "--verbose", action = "store_true",
                            help = "enable log messages during code generation")

        script.add_argument("--lazy-overwrite", action = "store_true",
                            help = "only overwrite output files when their"
                            " contents actually changed")

        paths.add_argument("out_path", type = file_path,
                           help = "path to write the header file to")
        paths.add_argument("def_paths", metavar = "def_path",
                           nargs = "+", type = file_path,
                           help = "paths to your enums.def files")

        return parser

    def __init__(self, args: "typing.Sequence[str] | None" = None):
        __class__.get_argparser().parse_args(args, namespace = self)

    def log_verbose(self, *args):
        """Print the given arguments iff verbose logging is enabled"""
        if self.verbose:
            print(*args)

    @property
    def _root_path(self) -> "Path | None":
        """Root Freeciv path, if we can find it."""
        path = Path(__file__).resolve()
        root = path.parent.parent
        if path != root / "gen_headers" / "generate_enums.py":
            self.log_verbose("Warning: couldn't find Freeciv root path")
            return None
        return root

    def _relative_path(self, path: Path) -> Path:
        """Find the relative path from the Freeciv root to the given path.
        Return the path unmodified if it's outside the Freeciv root, or if
        the Freeciv root could not be found."""
        root = self._root_path
        if root is not None:
            try:
                return path.resolve().relative_to(root)
            except ValueError:
                self.log_verbose(f"Warning: path {path} outside of Freeciv root")
        return path

    @property
    def _script_path(self) -> Path:
        """Relative path of the executed script. Under normal circumstances,
        this will be common/generate_packets.py, but it may differ when this
        module is imported from another script."""
        return self._relative_path(Path(sys.argv[0]))

    def _write_disclaimer(self, f: typing.TextIO):
        f.write(f"""\
 /**************************************************************************
 *                         THIS FILE WAS GENERATED                         *
 * Script: {self._script_path!s:63} *
""")

        for path in self.def_paths:
            f.write(f"""\
 * Input:  {self._relative_path(path)!s:63} *
""")

        f.write("""\
 *                         DO NOT CHANGE THIS FILE                         *
 **************************************************************************/

""")

    @contextmanager
    def _wrap_header(self, file: typing.TextIO, header_name: str) -> typing.Iterator[None]:
        """Add multiple inclusion protection to the given file"""
        name = f"FC__{header_name.upper()}_H"
        file.write(f"""\
#ifndef {name}
#define {name}

""")

        yield

        file.write(f"""\

#endif /* {name} */
""")

    @contextmanager
    def open_write(self, path: "str | Path") -> typing.Iterator[typing.TextIO]:
        """Open a file for writing, write a disclaimer and add multiple
        inclusion protection.

        If enabled, lazily overwrites the given file."""
        path = Path(path)   # no-op if path is already a Path object
        self.log_verbose(f"writing {path}")

        wrap_header = re.sub(r"[^\w]+", "_", path.name.split(".")[0]).upper()

        with ExitStack() as stack:
            if self.lazy_overwrite:
                file = stack.enter_context(self.lazy_overwrite_open(path))
            else:
                file = stack.enter_context(path.open("w"))

            self._write_disclaimer(file)
            stack.enter_context(self._wrap_header(file, wrap_header))

            yield file
        self.log_verbose(f"done writing {path}")

    @contextmanager
    def lazy_overwrite_open(self, path: "str | Path", suffix: str = ".tmp") -> typing.Iterator[typing.TextIO]:
        """Open a file for writing, but only actually overwrite it if the new
        content differs from the old content.

        This creates a temporary file by appending the given suffix to the given
        file path. In the event of an error, this temporary file might remain in
        the target file's directory."""

        path = Path(path)
        tmp_path = path.with_name(path.name + suffix)

        # if tmp_path already exists, assume it's left over from a previous,
        # failed run and can be overwritten without trouble
        self.log_verbose(f"lazy: using {tmp_path}")
        with tmp_path.open("w") as file:
            yield file

        if path.exists() and files_equal(tmp_path, path):
            self.log_verbose("lazy: no change, deleting...")
            tmp_path.unlink()
        else:
            self.log_verbose("lazy: content changed, replacing...")
            tmp_path.replace(path)


################### General helper functions and classes ###################

def files_equal(path_a: "str | Path", path_b: "str | Path") -> bool:
    """Return whether the contents of two text files are identical"""
    with Path(path_a).open() as file_a, Path(path_b).open() as file_b:
        return all(a == b for a, b in zip_longest(file_a, file_b))


class EnumValue:
    """Represents a single specenum constant (identifier and name)."""

    LINE_PATTERN = re.compile(r"""
        ^\s*
        (\w+)   # enum value identifier
        (?:
            \s+
            (                   # name (optional) - only capture
                \S+(?:\s+\S+)*  # the part starting and ending with
            )                   # non-whitespace
        )?
        \s*$
    """, re.VERBOSE)
    """Matches an enum value definition.

    Groups:
    - identifier
    - (optional) name"""

    identifier: str
    """The identifier (SPECENUM_VALUEx) for this constant"""

    name: "str | None"
    """The name (SPECENUM_VALUExNAME) for this constant"""

    @classmethod
    def parse(cls, line: str) -> "EnumValue":
        """Parse a single line defining an enum value"""
        mo = cls.LINE_PATTERN.fullmatch(line)
        if mo is None:
            raise ValueError(f"invalid enum value definition: {line!r}")
        return cls(mo.group(1), mo.group(2))

    def __init__(self, identifier: str, name: "str | None"):
        self.identifier = identifier
        self.name = name

    def code_parts_custom(self, value: str, prefix: str = "") -> typing.Iterable[str]:
        """Yield code defining this enum value for either a regular value,
        or special values like COUNT and ZERO."""
        yield f"""\
#define SPECENUM_{value} {prefix}{self.identifier}
"""
        if self.name is not None:
            yield f"""\
#define SPECENUM_{value}NAME {self.name}
"""

    def code_parts_value(self, index: int, prefix: str = "") -> typing.Iterable[str]:
        """Yield code defining this SPECENUM_VALUE"""
        return self.code_parts_custom(f"VALUE{index}", prefix)


# NB: avoid confusion with Python's Enum class
class Specenum:
    """Represents a single enum definition (i.e. a single use of specenum_gen.h)"""

    VALUES_SEP_PATTERN = re.compile(r"^\s*values\s*$")
    """Matches the "values" line separating options from enum values"""

    OPTION_PATTERN = re.compile(r"""
        ^\s*
        ([\w-]+)    # option name
        (?:
            \s+
            (                   # arguments (optional) - only capture
                \S+(?:\s+\S+)*  # the part starting and ending with
            )                   # non-whitespace
        )?
        \s*$
    """, re.VERBOSE)
    """Matches a single enum option.

    Groups:
    - the option name
    - (optional) the option's arguments"""

    GENERIC_PATTERN = re.compile(r"""
        ^\s*
        (\d+)   # amount
        \s+
        (\w+)   # identifier prefix
        \s*$
    """, re.VERBOSE)
    """Matches the arguments of a 'generic' enum option.

    Groups:
    - the number of generic values to generate
    - the identifier prefix"""

    DEFAULT_ZERO = EnumValue("ZERO", None)
    """Default SPECENUM_ZERO info when the 'zero' option is used without
    any identifier, but in conjunction with a 'prefix' option"""

    DEFAULT_COUNT = EnumValue("COUNT", None)
    """Default SPECENUM_COUNT info when the 'count' option is used without
    any identifier, but in conjunction with a 'prefix' option"""

    name: str
    """The SPECENUM_NAME of this enum"""

    prefix: str = ""
    """The prefix prepended to all value identifiers"""

    bitwise: bool = False
    """Whether this enum is bitwise"""

    zero: "EnumValue | None" = None
    """The SPECENUM_ZERO identifier and name, if given.
    Only valid if this enum is bitwise."""

    count: "EnumValue | None" = None
    """The SPECENUM_COUNT identifier and name, if given"""

    invalid: "str | None" = None
    """The SPECENUM_INVALID value, if given"""

    name_override: bool = False
    """Whether to request name override calls"""

    name_updater: bool = False
    """Whether to request name update calls"""

    bitvector: "str | None" = None
    """The SPECENUM_BITVECTOR name, if given"""

    values: "list[EnumValue]"
    """The values of this enum"""

    def __init__(self, name: str, lines: typing.Iterable[str]):
        self.name = name

        generic_amount: int = 0
        generic_prefix: str = ""

        lines_iter = iter(lines)

        for option_text in takewhile(
            lambda line: __class__.VALUES_SEP_PATTERN.fullmatch(line) is None,
            lines_iter,
        ):
            mo = __class__.OPTION_PATTERN.fullmatch(option_text)
            if mo is None:
                raise ValueError(f"malformed option for enum {self.name}: {option_text.strip()!r}")

            option: str = mo.group(1)
            arg: "str | None" = mo.group(2)

            if option == "bitvector":
                if self.bitvector is not None:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if arg is None:
                    raise ValueError(f"option {option!r} for enum {self.name} requires an argument")
                self.bitvector = arg
            elif option == "bitwise":
                if self.bitwise:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if arg is not None:
                    raise ValueError(f"option {option!r} for enum {self.name} does not support an argument")
                self.bitwise = True
            elif option == "count":
                if self.count is not None:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                self.count = __class__.DEFAULT_COUNT if arg is None else EnumValue.parse(arg)
            elif option == "generic":
                if generic_amount:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if not arg:
                    raise ValueError(f"option {option!r} for enum {self.name} requires an argument")
                mo_g = __class__.GENERIC_PATTERN.fullmatch(arg)
                if mo_g is None:
                    raise ValueError(f"malformed argument for option {option!r} of enum {self.name}")
                generic_amount = int(mo_g.group(1))
                if not generic_amount:
                    raise ValueError(f"amount for option {option!r} of enum {self.name} must be positive")
                generic_prefix = mo_g.group(2)
            elif option == "invalid":
                if self.invalid is not None:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if arg is None:
                    raise ValueError(f"option {option!r} for enum {self.name} requires an argument")
                self.invalid = arg
            elif option == "name-override":
                if self.name_override:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if arg is not None:
                    raise ValueError(f"option {option!r} for enum {self.name} does not support an argument")
                self.name_override = True
            elif option == "name-updater":
                if self.name_updater:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if arg is not None:
                    raise ValueError(f"option {option!r} for enum {self.name} does not support an argument")
                self.name_updater = True
            elif option == "prefix":
                if self.prefix:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                if not arg:
                    raise ValueError(f"option {option!r} for enum {self.name} requires an argument")
                self.prefix = arg
            elif option == "zero":
                if self.zero is not None:
                    raise ValueError(f"duplicate option {option!r} for enum {self.name}")
                self.zero = __class__.DEFAULT_ZERO if arg is None else EnumValue.parse(arg)
            else:
                raise ValueError(f"unrecognized option {option!r} for enum {self.name}")

        # check validity
        if self.zero and not self.bitwise:
            raise ValueError(f"option 'zero' for enum {self.name} requires option 'bitwise'")
        if self.count and self.bitwise:
            raise ValueError(f"option 'count' conflicts with option 'bitwise' for enum {self.name}")
        if self.bitvector and self.bitwise:
            raise ValueError(f"option 'bitvector' conflicts with option 'bitwise' for enum {self.name}")

        # check sanity
        if self.zero is __class__.DEFAULT_ZERO and not self.prefix:
            raise ValueError(f"option 'zero' for enum {self.name} requires an argument or option 'prefix'")
        if self.count is __class__.DEFAULT_COUNT and not self.prefix:
            raise ValueError(f"option 'count' for enum {self.name} requires an argument or option 'prefix'")

        self.values = [
            EnumValue.parse(line) for line in lines
        ] + [
            EnumValue(generic_prefix + str(i), None)
            for i in range(1, generic_amount + 1)
        ]

    def code_parts(self) -> typing.Iterable[str]:
        """Yield code defining this enum"""
        yield f"""\
#define SPECENUM_NAME {self.name}
"""

        if self.bitwise:
            yield f"""\
#define SPECENUM_BITWISE
"""
            if self.zero is not None:
                yield from self.zero.code_parts_custom("ZERO", self.prefix)

        for i, value in enumerate(self.values):
            yield from value.code_parts_value(i, self.prefix)

        if self.count is not None:
            yield from self.count.code_parts_custom("COUNT", self.prefix)
        if self.invalid is not None:
            yield f"""\
#define SPECENUM_INVALID {self.invalid}
"""
        if self.name_override:
            yield f"""\
#define SPECENUM_NAMEOVERRIDE
"""
        if self.name_updater:
            yield f"""\
#define SPECENUM_NAME_UPDATER
"""
        if self.bitvector is not None:
            yield f"""\
#define SPECENUM_BITVECTOR {self.bitvector}
"""
        yield f"""\
#include "specenum_gen.h"
"""


class EnumsDefinition(typing.Iterable[Specenum]):
    """Represents an entire enums definition file"""

    COMMENT_START_PATTERN = re.compile(r"""
        ^\s*    # strip initial whitespace
        (.*?)   # actual content; note the reluctant quantifier
        \s*     # note: this can cause quadratic backtracking
        (?:     # match a potential comment
            (?:     # EOL comment (or just EOL)
                (?:
                    (?:\#|//)   # opening # or //
                    .*
                )?
            ) | (?: # block comment ~> capture remaining text
                /\*     # opening /*
                [^*]*   # text that definitely can't end the block comment
                (.*)    # remaining text, might contain a closing */
            )
        )
        (?:\n)? # optional newline in case those aren't stripped
        $
    """, re.VERBOSE)
    """Used to clean lines when not starting inside a block comment. Finds
    the start of a block comment, if it exists.

    Groups:
    - Actual content before any comment starts; stripped.
    - Remaining text after the start of a block comment. Not present if no
      block comment starts on this line."""

    COMMENT_END_PATTERN = re.compile(r"""
        ^
        .*?     # comment; note the reluctant quantifier
        (?:     # end of block comment ~> capture remaining text
            \*/     # closing */
            \s*     # strip whitespace after comment
            (.*)    # remaining text
        )?
        (?:\n)? # optional newline in case those aren't stripped
        $
    """, re.VERBOSE)
    """Used to clean lines when starting inside a block comment. Finds the
    end of a block comment, if it exists.

    Groups:
    - Remaining text after the end of the block comment; lstripped. Not
      present if the block comment doesn't end on this line."""

    ENUM_HEADER_PATTERN = re.compile(r"""
        ^\s*
        enum
        \s+
        (\w+)       # enum name
        \s*
        (?:;\s*)?   # optional semicolon (nothing comes after it yet)
        $
    """, re.VERBOSE)
    """Matches the header line of an enum definition

    Groups:
    - enum name"""

    ENUM_END_PATTERN = re.compile(r"^\s*end\s*$")
    """Matches the "end" line terminating an enum definition"""

    cfg: ScriptConfig
    """Configuration used for code generated from this definition"""

    enums: "list[Specenum]"
    """List of all defined enums, in order of definition"""

    enums_by_name: "dict[str, Specenum]"
    """Maps enum names to their enum definition"""

    @classmethod
    def _clean_lines(cls, lines: typing.Iterable[str]) -> typing.Iterator[str]:
        """Strip comments and leading/trailing whitespace from the given
        lines. If a block comment starts in one line and ends in another,
        the remaining parts are joined together and yielded as one line."""
        inside_comment = False
        parts = []

        for line in lines:
            while line:
                if inside_comment:
                    # currently inside a block comment ~> look for */
                    mo = cls.COMMENT_END_PATTERN.fullmatch(line)
                    assert mo, repr(line)
                    # If the group wasn't captured (None), we haven't found
                    # a */ to end our comment ~> still inside_comment
                    # Otherwise, group captured remaining line content
                    line, = mo.groups(None)
                    inside_comment = line is None
                else:
                    mo = cls.COMMENT_START_PATTERN.fullmatch(line)
                    assert mo, repr(line)
                    # If the second group wasn't captured (None), there is
                    # no /* to start a block comment ~> not inside_comment
                    part, line = mo.groups(None)
                    inside_comment = line is not None
                    if part: parts.append(part)

            if (not inside_comment) and parts:
                # when ending a line outside a block comment, yield what
                # we've accumulated
                yield " ".join(parts)
                parts.clear()

        if inside_comment:
            raise ValueError("EOF while scanning block comment")

    def parse_lines(self, lines: typing.Iterable[str]):
        """Parse the given lines as type and packet definitions."""
        self.parse_clean_lines(self._clean_lines(lines))

    def parse_clean_lines(self, lines: typing.Iterable[str]):
        """Parse the given lines as specenum definitions. Comments
        and blank lines must already be removed beforehand."""
        # hold on to the iterator itself
        lines_iter = iter(lines)
        for line in lines_iter:
            mo = self.ENUM_HEADER_PATTERN.fullmatch(line)
            if mo is not None:
                enum_name, = mo.groups("")

                if enum_name in self.enums_by_name:
                    raise ValueError(f"Duplicate enum name: {enum_name}")

                enum = Specenum(
                    enum_name,
                    takewhile(
                        lambda line: self.ENUM_END_PATTERN.fullmatch(line) is None,
                        lines_iter, # advance the iterator used by this for loop
                    ),
                )

                self.enums.append(enum)
                self.enums_by_name[enum_name] = enum
                continue

            raise ValueError(f"Unexpected line: {line}")

    def __init__(self, cfg: ScriptConfig):
        self.cfg = cfg
        self.enums = []
        self.enums_by_name = {}

    def __iter__(self) -> typing.Iterator[Specenum]:
        return iter(self.enums)


########################### Writing output files ###########################

def write_header(path: "str | Path | None", enums: EnumsDefinition):
    """Write a header with the defined enums to the given path"""
    if path is None:
        return
    with enums.cfg.open_write(path) as output_h:
        for specenum in enums:
            output_h.write("\n")
            output_h.writelines(specenum.code_parts())


def main(raw_args: "typing.Sequence[str] | None" = None):
    """Main function. Read the given arguments, or the command line
    arguments if raw_args is not given, and run the specenum code generation
    script accordingly."""
    script_args = ScriptConfig(raw_args)

    enums = EnumsDefinition(script_args)
    for path in script_args.def_paths:
        with path.open() as input_file:
            enums.parse_lines(input_file)

    write_header(script_args.out_path, enums)


if __name__ == "__main__":
    main()
