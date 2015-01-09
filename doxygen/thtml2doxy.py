#!/usr/bin/env python

## @package thtml2doxy_clang
#  Translates THtml C++ comments to Doxygen using libclang as parser.
#
#  This code relies on Python bindings for libclang: libclang's interface is pretty unstable, and
#  its Python bindings are unstable as well.
#
#  AST (Abstract Source Tree) traversal is performed entirely using libclang used as a C++ parser,
#  instead of attempting to write a parser ourselves.
#
#  This code (expecially AST traversal) was inspired by:
#
#   - [Implementing a code generator with libclang](http://szelei.me/code-generator/)
#     (this refers to API calls used here)
#   - [Parsing C++ in Python with Clang](http://eli.thegreenplace.net/2011/07/03/parsing-c-in-python-with-clang)
#     (outdated, API calls described there do not work anymore, but useful to understand some basic
#     concepts)
#
#  Usage:
#
#    `thtml2doxy_clang [--stdout|-o] [-d] [--debug=DEBUG_LEVEL] file1 [file2 [file3...]]`
#
#  Parameters:
#
#   - `--stdout|-o`: output all on standard output instead of writing files in place
#   - `-d`: enable debug mode (very verbose output)
#   - `--debug=DEBUG_LEVEL`: set debug level to one of `DEBUG`, `INFO`, `WARNING`, `ERROR`,
#     `CRITICAL`
#
#  @author Dario Berzano, CERN
#  @date 2014-12-05


import sys
import os
import re
import logging
import getopt
import hashlib
import clang.cindex


## Brain-dead color output for terminal.
class Colt(str):

  def red(self):
    return self.color('\033[31m')

  def green(self):
    return self.color('\033[32m')

  def yellow(self):
    return self.color('\033[33m')

  def blue(self):
    return self.color('\033[34m')

  def magenta(self):
    return self.color('\033[35m')

  def cyan(self):
    return self.color('\033[36m')

  def color(self, c):
    return c + self + '\033[m'


## Comment.
class Comment:

  def __init__(self, lines, first_line, first_col, last_line, last_col, indent, func):
    assert first_line > 0 and last_line >= first_line, 'Wrong line numbers'
    self.lines = lines
    self.first_line = first_line
    self.first_col = first_col
    self.last_line = last_line
    self.last_col = last_col
    self.indent = indent
    self.func = func

  def has_comment(self, line):
    return line >= self.first_line and line <= self.last_line

  def __str__(self):
    return "<Comment for %s: [%d,%d:%d,%d] %s>" % (self.func, self.first_line, self.first_col, self.last_line, self.last_col, self.lines)


## A data member comment.
class MemberComment:

  def __init__(self, text, comment_flag, array_size, first_line, first_col, func):
    assert first_line > 0, 'Wrong line number'
    assert comment_flag is None or comment_flag == '!' or comment_flag in [ '!', '||', '->' ]
    self.lines = [ text ]
    self.comment_flag = comment_flag
    self.array_size = array_size
    self.first_line = first_line
    self.first_col = first_col
    self.func = func

  def is_transient(self):
    return self.comment_flag == '!'

  def is_dontsplit(self):
    return self.comment_flag == '||'

  def is_ptr(self):
    return self.comment_flag == '->'

  def has_comment(self, line):
    return line == self.first_line

  def __str__(self):

    if self.is_transient():
      tt = '!transient! '
    elif self.is_dontsplit():
      tt = '!dontsplit! '
    elif self.is_ptr():
      tt = '!ptr! '
    else:
      tt = ''

    if self.array_size is not None:
      ars = '[%s] ' % self.array_size
    else:
      ars = ''

    return "<MemberComment for %s: [%d,%d] %s%s%s>" % (self.func, self.first_line, self.first_col, tt, ars, self.lines[0])


## A dummy comment that removes comment lines.
class RemoveComment(Comment):

  def __init__(self, first_line, last_line):
    assert first_line > 0 and last_line >= first_line, 'Wrong line numbers'
    self.first_line = first_line
    self.last_line = last_line
    self.func = '<remove>'

  def __str__(self):
    return "<RemoveComment: [%d,%d]>" % (self.first_line, self.last_line)


## Parses method comments.
#
#  @param cursor   Current libclang parser cursor
#  @param comments Array of comments: new ones will be appended there
def comment_method(cursor, comments):

  # we are looking for the following structure: method -> compound statement -> comment, i.e. we
  # need to extract the first comment in the compound statement composing the method

  in_compound_stmt = False
  expect_comment = False
  emit_comment = False

  comment = []
  comment_function = cursor.spelling or cursor.displayname
  comment_line_start = -1
  comment_line_end = -1
  comment_col_start = -1
  comment_col_end = -1
  comment_indent = -1

  for token in cursor.get_tokens():

    if token.cursor.kind == clang.cindex.CursorKind.COMPOUND_STMT:
      if not in_compound_stmt:
        in_compound_stmt = True
        expect_comment = True
        comment_line_end = -1
    else:
      if in_compound_stmt:
        in_compound_stmt = False
        emit_comment = True

    # tkind = str(token.kind)[str(token.kind).index('.')+1:]
    # ckind = str(token.cursor.kind)[str(token.cursor.kind).index('.')+1:]

    if in_compound_stmt:

      if expect_comment:

        extent = token.extent
        line_start = extent.start.line
        line_end = extent.end.line

        if token.kind == clang.cindex.TokenKind.PUNCTUATION and token.spelling == '{':
          pass

        elif token.kind == clang.cindex.TokenKind.COMMENT and (comment_line_end == -1 or (line_start == comment_line_end+1 and line_end-line_start == 0)):
          comment_line_end = line_end
          comment_col_end = extent.end.column

          if comment_indent == -1 or (extent.start.column-1) < comment_indent:
            comment_indent = extent.start.column-1

          if comment_line_start == -1:
            comment_line_start = line_start
            comment_col_start = extent.start.column
          comment.extend( token.spelling.split('\n') )

          # multiline comments are parsed in one go, therefore don't expect subsequent comments
          if line_end - line_start > 0:
            emit_comment = True
            expect_comment = False

        else:
          emit_comment = True
          expect_comment = False

    if emit_comment:

      if comment_line_start > 0:

        comment = refactor_comment( comment, infilename=str(cursor.location.file) )

        if len(comment) > 0:
          logging.debug("Comment found for function %s" % Colt(comment_function).magenta())
          comments.append( Comment(comment, comment_line_start, comment_col_start, comment_line_end, comment_col_end, comment_indent, comment_function) )
        else:
          logging.debug('Empty comment found for function %s: collapsing' % Colt(comment_function).magenta())
          comments.append( Comment([''], comment_line_start, comment_col_start, comment_line_end, comment_col_end, comment_indent, comment_function) )
          #comments.append(RemoveComment(comment_line_start, comment_line_end))

      else:
        logging.warning('No comment found for function %s' % Colt(comment_function).magenta())

      comment = []
      comment_line_start = -1
      comment_line_end = -1
      comment_col_start = -1
      comment_col_end = -1
      comment_indent = -1

      emit_comment = False
      break


## Parses comments to class data members.
#
#  @param cursor   Current libclang parser cursor
#  @param comments Array of comments: new ones will be appended there
def comment_datamember(cursor, comments):

  # Note: libclang 3.5 seems to have problems parsing a certain type of FIELD_DECL, so we revert
  # to a partial manual parsing. When parsing fails, the cursor's "extent" is not set properly,
  # returning a line range 0-0. We therefore make the not-so-absurd assumption that the datamember
  # definition is fully on one line, and we take the line number from cursor.location.

  line_num = cursor.location.line
  raw = None
  prev = None
  found = False

  # Huge overkill: current line saved in "raw", previous in "prev"
  with open(str(cursor.location.file)) as fp:
    cur_line = 0
    for raw in fp:
      cur_line = cur_line + 1
      if cur_line == line_num:
        found = True
        break
      prev = raw

  assert found, 'A line that should exist was not found in file' % cursor.location.file

  recomm = r'(//(!|\|\||->)|///?)(\[([0-9,]+)\])?<?\s*(.*?)\s*$'
  recomm_prevline = r'^\s*///\s*(.*?)\s*$'

  mcomm = re.search(recomm, raw)
  if mcomm:
    # If it does not match, we do not have a comment
    member_name = cursor.spelling;
    comment_flag = mcomm.group(2)
    array_size = mcomm.group(4)
    text = mcomm.group(5)

    col_num = mcomm.start()+1;

    if array_size is not None and prev is not None:
      # ROOT arrays with comments already converted to Doxygen have the member description on the
      # previous line
      mcomm_prevline = re.search(recomm_prevline, prev)
      if mcomm_prevline:
        text = mcomm_prevline.group(1)
        comments.append(RemoveComment(line_num-1, line_num-1))

    logging.debug('Comment found for member %s' % Colt(member_name).magenta())

    comments.append( MemberComment(
      text,
      comment_flag,
      array_size,
      line_num,
      col_num,
      member_name ))


## Parses class description (beginning of file).
#
#  The clang parser does not work in this case so we do it manually, but it is very simple: we keep
#  the first consecutive sequence of single-line comments (//) we find - provided that it occurs
#  before any other comment found so far in the file (the comments array is inspected to ensure
#  this).
#
#  Multi-line comments (/* ... */) are not considered as they are commonly used to display
#  copyright notice.
#
#  @param filename Name of the current file
#  @param comments Array of comments: new ones will be appended there
def comment_classdesc(filename, comments):

  recomm = r'^\s*///?(\s*.*?)\s*/*\s*$'

  reclass_doxy = r'(?i)^\s*\\(class|file):?\s*([^.]*)'
  class_name_doxy = None

  reauthor = r'(?i)^\s*\\?authors?:?\s*(.*?)\s*(,?\s*([0-9./-]+))?\s*$'
  redate = r'(?i)^\s*\\?date:?\s*([0-9./-]+)\s*$'
  author = None
  date = None

  comment_lines = []

  start_line = -1
  end_line = -1

  line_num = 0

  is_macro = filename.endswith('.C')

  with open(filename, 'r') as fp:

    for raw in fp:

      line_num = line_num + 1

      if raw.strip() == '' and start_line > 0:
        # Skip empty lines
        continue

      stripped = strip_html(raw)
      mcomm = re.search(recomm, stripped)
      if mcomm:

        if start_line == -1:

          # First line. Check that we do not overlap with other comments
          comment_overlaps = False
          for c in comments:
            if c.has_comment(line_num):
              comment_overlaps = True
              break

          if comment_overlaps:
            # No need to look for other comments
            break

          start_line = line_num

        end_line = line_num
        append = True

        mclass_doxy = re.search(reclass_doxy, mcomm.group(1))
        if mclass_doxy:
          class_name_doxy = mclass_doxy.group(2)
          append = False
        else:
          mauthor = re.search(reauthor, mcomm.group(1))
          if mauthor:
            author = mauthor.group(1)
            if date is None:
              # Date specified in the standalone \date field has priority
              date = mauthor.group(3)
            append = False
          else:
            mdate = re.search(redate, mcomm.group(1))
            if mdate:
              date = mdate.group(1)
              append = False

        if append:
          comment_lines.append( mcomm.group(1) )

      else:
        if start_line > 0:
          break

  if class_name_doxy is None:

    # No \class specified: guess it from file name
    reclass = r'^(.*/)?(.*?)(\..*)?$'
    mclass = re.search( reclass, filename )
    if mclass:
      class_name_doxy = mclass.group(2)
    else:
      assert False, 'Regexp unable to extract classname from file'

  if start_line > 0:

    # Prepend \class or \file specifier (and an empty line)
    if is_macro:
      comment_lines[:0] = [ '\\file ' + class_name_doxy + '.C' ]
    else:
      comment_lines[:0] = [ '\\class ' + class_name_doxy ]

    # Append author and date if they exist
    comment_lines.append('')

    if author is not None:
      comment_lines.append( '\\author ' + author )

    if date is not None:
      comment_lines.append( '\\date ' + date )

    comment_lines = refactor_comment(comment_lines, do_strip_html=False, infilename=filename)
    logging.debug('Comment found for class %s' % Colt(class_name_doxy).magenta())
    comments.append(Comment(
      comment_lines,
      start_line, 1, end_line, 1,
      0, class_name_doxy
    ))

  else:

    logging.warning('No comment found for class %s' % Colt(class_name_doxy).magenta())


## Traverse the AST recursively starting from the current cursor.
#
#  @param cursor    A Clang parser cursor
#  @param filename  Name of the current file
#  @param comments  Array of comments: new ones will be appended there
#  @param recursion Current recursion depth
def traverse_ast(cursor, filename, comments, recursion=0):

  # libclang traverses included files as well: we do not want this behavior
  if cursor.location.file is not None and str(cursor.location.file) != filename:
    logging.debug("Skipping processing of included %s" % cursor.location.file)
    return

  text = cursor.spelling or cursor.displayname
  kind = str(cursor.kind)[str(cursor.kind).index('.')+1:]

  is_macro = filename.endswith('.C')

  indent = ''
  for i in range(0, recursion):
    indent = indent + '  '

  if cursor.kind in [ clang.cindex.CursorKind.CXX_METHOD, clang.cindex.CursorKind.CONSTRUCTOR,
    clang.cindex.CursorKind.DESTRUCTOR ]:

    # cursor ran into a C++ method
    logging.debug( "%5d %s%s(%s)" % (cursor.location.line, indent, Colt(kind).magenta(), Colt(text).blue()) )
    comment_method(cursor, comments)

  elif not is_macro and cursor.kind in [ clang.cindex.CursorKind.FIELD_DECL, clang.cindex.CursorKind.VAR_DECL ]:

    # cursor ran into a data member declaration
    logging.debug( "%5d %s%s(%s)" % (cursor.location.line, indent, Colt(kind).magenta(), Colt(text).blue()) )
    comment_datamember(cursor, comments)

  else:

    logging.debug( "%5d %s%s(%s)" % (cursor.location.line, indent, kind, text) )

  for child_cursor in cursor.get_children():
    traverse_ast(child_cursor, filename, comments, recursion+1)

  if recursion == 0:
    comment_classdesc(filename, comments)


## Strip some HTML tags from the given string. Returns clean string.
#
#  @param s Input string
def strip_html(s):
  rehtml = r'(?i)</?(P|BR)/?>'
  return re.sub(rehtml, '', s)


## Remove garbage from comments and convert special tags from THtml to Doxygen.
#
#  @param comment An array containing the lines of the original comment
def refactor_comment(comment, do_strip_html=True, infilename=None):

  recomm = r'^(/{2,}|/\*)? ?(\s*.*?)\s*((/{2,})?\s*|\*/)$'
  regarbage = r'^(?i)\s*([\s*=-_#]+|(Begin|End)_Html)\s*$'

  # Support for LaTeX blocks spanning on multiple lines
  relatex = r'(?i)^((.*?)\s+)?(BEGIN|END)_LATEX([.,;:\s]+.*)?$'
  in_latex = False
  latex_block = False

  # Support for LaTeX blocks on a single line
  reinline_latex = r'(?i)(.*)BEGIN_LATEX\s+(.*?)\s+END_LATEX(.*)$'

  # Match <pre> (to turn it into the ~~~ Markdown syntax)
  reblock = r'(?i)^(\s*)</?PRE>\s*$'

  # Macro blocks for pictures generation
  in_macro = False
  current_macro = []
  remacro = r'(?i)^\s*(BEGIN|END)_MACRO(\((.*?)\))?\s*$'

  new_comment = []
  insert_blank = False
  wait_first_non_blank = True
  for line_comment in comment:

    # Check if we are in a macro block
    mmacro = re.search(remacro, line_comment)
    if mmacro:
      if in_macro:
        in_macro = False

        # Dump macro
        outimg = write_macro(infilename, current_macro) + '.png'
        current_macro = []

        # Insert image
        new_comment.append( '![Picture from ROOT macro](%s)' % (outimg) )

        logging.debug( 'Found macro for generating image %s' % Colt(outimg).magenta() )

      else:
        in_macro = True

      continue
    elif in_macro:
      current_macro.append( line_comment )
      continue

    # Strip some HTML tags
    if do_strip_html:
      line_comment = strip_html(line_comment)

    mcomm = re.search( recomm, line_comment )
    if mcomm:
      new_line_comment = mcomm.group(2)
      mgarbage = re.search( regarbage, new_line_comment )

      if new_line_comment == '' or mgarbage is not None:
        insert_blank = True
      else:
        if insert_blank and not wait_first_non_blank:
          new_comment.append('')
        insert_blank = False
        wait_first_non_blank = False

        # Postprocessing: LaTeX formulas in ROOT format
        # Marked by BEGIN_LATEX ... END_LATEX and they use # in place of \
        # There can be several ROOT LaTeX forumlas per line
        while True:
          minline_latex = re.search( reinline_latex, new_line_comment )
          if minline_latex:
            new_line_comment = '%s\\f$%s\\f$%s' % \
              ( minline_latex.group(1), minline_latex.group(2).replace('#', '\\'),
                minline_latex.group(3) )
          else:
            break

        # ROOT LaTeX: do we have a Begin/End_LaTeX block?
        # Note: the presence of LaTeX "closures" does not exclude the possibility to have a begin
        # block here left without a corresponding ending block
        mlatex = re.search( relatex, new_line_comment )
        if mlatex:

          # before and after parts have been already stripped
          l_before = mlatex.group(2)
          l_after = mlatex.group(4)
          is_begin = mlatex.group(3).upper() == 'BEGIN'  # if not, END

          if l_before is None:
            l_before = ''
          if l_after is None:
            l_after = ''

          if is_begin:

            # Begin of LaTeX part

            in_latex = True
            if l_before == '' and l_after == '':

              # Opening tag alone: mark the beginning of a block: \f[ ... \f]
              latex_block = True
              new_comment.append( '\\f[' )

            else:
              # Mark the beginning of inline: \f$ ... \f$
              latex_block = False
              new_comment.append(
                '%s \\f$%s' % ( l_before, l_after.replace('#', '\\') )
              )

          else:

            # End of LaTeX part
            in_latex = False

            if latex_block:

              # Closing a LaTeX block
              if l_before != '':
                new_comment.append( l_before.replace('#', '\\') )
              new_comment.append( '\\f]' )
              if l_after != '':
                new_comment.append( l_after )

            else:

              # Closing a LaTeX inline
              new_comment.append(
                '%s\\f$%s' % ( l_before.replace('#', '\\'), l_after )
              )

          # Prevent appending lines (we have already done that)
          new_line_comment = None

        # If we are not in a LaTeX block, look for <pre> tags and transform them into Doxygen code
        # blocks (using ~~~ ... ~~~). Only <pre> tags on a single line are supported
        if new_line_comment is not None and not in_latex:

          mblock = re.search( reblock, new_line_comment  )
          if mblock:
            new_comment.append( mblock.group(1)+'~~~' )
            new_line_comment = None

        if new_line_comment is not None:
          if in_latex:
            new_line_comment = new_line_comment.replace('#', '\\')
          new_comment.append( new_line_comment )

    else:
      assert False, 'Comment regexp does not match'

  return new_comment


## Dumps an image-generating macro to the correct place. Returns a string with the image path,
#  without the extension.
#
#  @param infilename  File name of the source file
#  @param macro_lines Array of macro lines
def write_macro(infilename, macro_lines):

  # Calculate hash
  digh = hashlib.sha1()
  for l in macro_lines:
    digh.update(l)
    digh.update('\n')
  short_digest = digh.hexdigest()[0:7]

  outdir = '%s/imgdoc' % os.path.dirname(infilename)
  outprefix = '%s/%s_%s' % (
    outdir,
    os.path.basename(infilename).replace('.', '_'),
    short_digest
  )
  outmacro = '%s.C' % outprefix

  # Make directory
  if not os.path.isdir(outdir):
    # do not catch: let everything die on error
    logging.debug('Creating directory %s' % Colt(outdir).magenta())
    os.mkdir(outdir)

  # Create file (do not catch errors either)
  with open(outmacro, 'w') as omfp:
    logging.debug('Writing macro %s' % Colt(outmacro).magenta())
    for l in macro_lines:
      omfp.write(l)
      omfp.write('\n')

  return outprefix


## Rewrites all comments from the given file handler.
#
#  @param fhin     The file handler to read from
#  @param fhout    The file handler to write to
#  @param comments Array of comments
def rewrite_comments(fhin, fhout, comments):

  line_num = 0
  in_comment = False
  skip_empty = False
  comm = None
  prev_comm = None

  rindent = r'^(\s*)'


  def dump_comment_block(cmt):
   text_indent = ''
   for i in range(0, cmt.indent):
     text_indent = text_indent + ' '

   for lc in cmt.lines:
     fhout.write( "%s/// %s\n" % (text_indent, lc) );
   fhout.write('\n')


  for line in fhin:

    line_num = line_num + 1

    # Find current comment
    prev_comm = comm
    comm = None
    for c in comments:
      if c.has_comment(line_num):
        comm = c

    if comm:

      if isinstance(comm, MemberComment):

        # end comment block
        if in_comment:
          dump_comment_block(prev_comm)
          in_comment = False

        non_comment = line[ 0:comm.first_col-1 ]

        if comm.array_size is not None or comm.is_dontsplit() or comm.is_ptr():

          # This is a special case: comment will be split in two lines: one before the comment for
          # Doxygen as "member description", and the other right after the comment on the same line
          # to be parsed by ROOT's C++ parser

          # Keep indent on the generated line of comment before member definition
          mindent = re.search(rindent, line)

          # Get correct comment flag, if any
          if comm.comment_flag is not None:
            cflag = comm.comment_flag
          else:
            cflag = ''

          # Get correct array size, if any
          if comm.array_size is not None:
            asize = '[%s]' % comm.array_size
          else:
            asize = ''

          # Write on two lines
          fhout.write('%s/// %s\n%s//%s%s\n' % (
            mindent.group(1),
            comm.lines[0],
            non_comment,
            cflag,
            asize
          ))

        else:

          # Single-line comments with the "transient" flag can be kept on one line in a way that
          # they are correctly interpreted by both ROOT and Doxygen

          if comm.is_transient():
            tt = '!'
          else:
            tt = '/'

          fhout.write('%s//%s< %s\n' % (
            non_comment,
            tt,
            comm.lines[0]
          ))

      elif isinstance(comm, RemoveComment):
        # End comment block and skip this line
        if in_comment:
          dump_comment_block(prev_comm)
          in_comment = False

      elif prev_comm is None:

        # Beginning of a new comment block of type Comment
        in_comment = True

        # Extract the non-comment part and print it if it exists
        non_comment = line[ 0:comm.first_col-1 ].rstrip()
        if non_comment != '':
          fhout.write( non_comment + '\n' )

    else:

      if in_comment:

        # We have just exited a comment block of type Comment
        dump_comment_block(prev_comm)
        in_comment = False
        skip_empty = True

      line_out = line.rstrip('\n')
      if skip_empty:
        skip_empty = False
        if line_out.strip() != '':
          fhout.write( line_out + '\n' )
      else:
        fhout.write( line_out + '\n' )


## The main function.
#
#  Return value is the executable's return value.
def main(argv):

  # Setup logging on stderr
  log_level = logging.INFO
  logging.basicConfig(
    level=log_level,
    format='%(levelname)-8s %(funcName)-20s %(message)s',
    stream=sys.stderr
  )

  # Parse command-line options
  output_on_stdout = False
  include_flags = []
  try:
    opts, args = getopt.getopt( argv, 'odI:', [ 'debug=', 'stdout' ] )
    for o, a in opts:
      if o == '--debug':
        log_level = getattr( logging, a.upper(), None )
        if not isinstance(log_level, int):
          raise getopt.GetoptError('log level must be one of: DEBUG, INFO, WARNING, ERROR, CRITICAL')
      elif o == '-d':
        log_level = logging.DEBUG
      elif o == '-o' or o == '--stdout':
        output_on_stdout = True
      elif o == '-I':
        if os.path.isdir(a):
          include_flags.extend( [ '-I', a ] )
        else:
          logging.fatal('Include directory not found: %s' % Colt(a).magenta())
          return 2
      else:
        assert False, 'Unhandled argument'
  except getopt.GetoptError as e:
    logging.fatal('Invalid arguments: %s' % e)
    return 1

  logging.getLogger('').setLevel(log_level)

  # Attempt to load libclang from a list of known locations
  libclang_locations = [
    '/usr/lib/llvm-3.5/lib/libclang.so.1',
    '/usr/lib/libclang.so',
    '/Library/Developer/CommandLineTools/usr/lib/libclang.dylib'
  ]
  libclang_found = False

  for lib in libclang_locations:
    if os.path.isfile(lib):
      clang.cindex.Config.set_library_file(lib)
      libclang_found = True
      break

  if not libclang_found:
    logging.fatal('Cannot find libclang')
    return 1

  # Loop over all files
  for fn in args:

    logging.info('Input file: %s' % Colt(fn).magenta())
    index = clang.cindex.Index.create()
    clang_args = [ '-x', 'c++' ]
    clang_args.extend( include_flags )
    translation_unit = index.parse(fn, args=clang_args)

    comments = []
    traverse_ast( translation_unit.cursor, fn, comments )
    for c in comments:

      logging.debug("Comment found for entity %s:" % Colt(c.func).magenta())

      if isinstance(c, MemberComment):

        if c.is_transient():
          flag_text = Colt('transient ').yellow()
        elif c.is_dontsplit():
          flag_text = Colt('dontsplit ').yellow()
        elif c.is_ptr():
          flag_text = Colt('ptr ').yellow()
        else:
          flag_text = ''

        if c.array_size is not None:
          array_text = Colt('arraysize=%s ' % c.array_size).yellow()
        else:
          array_text = ''

        logging.debug(
          "%s %s%s{%s}" % ( \
            Colt("[%d,%d]" % (c.first_line, c.first_col)).green(),
            flag_text,
            array_text,
            Colt(c.lines[0]).cyan()
        ))

      elif isinstance(c, RemoveComment):

        logging.debug( Colt('[%d,%d]' % (c.first_line, c.last_line)).green() )

      else:
        for l in c.lines:
          logging.debug(
            Colt("[%d,%d:%d,%d] " % (c.first_line, c.first_col, c.last_line, c.last_col)).green() +
            "{%s}" % Colt(l).cyan()
          )

    try:

      if output_on_stdout:
        with open(fn, 'r') as fhin:
          rewrite_comments( fhin, sys.stdout, comments )
      else:
        fn_back = fn + '.thtml2doxy_backup'
        os.rename( fn, fn_back )

        with open(fn_back, 'r') as fhin, open(fn, 'w') as fhout:
          rewrite_comments( fhin, fhout, comments )

        os.remove( fn_back )
        logging.info("File %s converted to Doxygen: check differences before committing!" % Colt(fn).magenta())
    except (IOError,OSError) as e:
      logging.error('File operation failed: %s' % e)

  return 0


if __name__ == '__main__':
  sys.exit( main( sys.argv[1:] ) )
