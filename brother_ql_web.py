#!/usr/bin/env python

"""
This is a web-based service to print labels on Brother QL label printers.
"""

import sys
import logging
import random
import json
import argparse
from io import BytesIO

from bottle import run, route, get, post, response, request, jinja2_view as view, static_file, redirect, re
from PIL import Image, ImageDraw, ImageFont

from brother_ql.devicedependent import models, label_type_specs, label_sizes
from brother_ql.devicedependent import ENDLESS_LABEL, DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL
from brother_ql import BrotherQLRaster, create_label
from brother_ql.backends import backend_factory, guess_backend

from font_helpers import get_fonts

# command line parameters
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', default=False)
parser.add_argument('--loglevel', default=False, type=lambda x: getattr(logging, x.upper()), help='Must be one of the following values: DEBUG, INFO, WARNING, ERROR, CRITICAL (default: WARNING')
parser.add_argument('--font-folder', default=False, help='Folder for additional .ttf/.otf fonts')
parser.add_argument('--default-label-size', choices=label_sizes, default=False, help='Label size inserted in your printer. (default: 62)')
parser.add_argument('--default-orientation', default=False, choices=('standard', 'rotated'), help='Label orientation; to turn your text by 90°, state "rotated" (default: standard')
parser.add_argument('--model', default=False, choices=models, help='The model of your printer (default: QL-500)')
parser.add_argument('printer',  nargs='?', default=False, help='String descriptor for the printer to use (like tcp://192.168.0.23:9100 or file:///dev/usb/lp0)')
args = parser.parse_args()

# load config
try:
    with open('config.json') as fh:
        CONFIG = json.load(fh)
except FileNotFoundError as e:
    with open('config.example.json') as fh:
        CONFIG = json.load(fh)

# if set, overwrite config values with cli parameters
    if args.printer:
        CONFIG['PRINTER']['PRINTER'] = args.printer

    if args.port:
        CONFIG['SERVER']['PORT'] = args.port

    if args.loglevel:
        CONFIG['SERVER']['LOGLEVEL'] = args.loglevel

    if CONFIG['SERVER']['LOGLEVEL'] == 'DEBUG':
        DEBUG = True
    else:
        DEBUG = False

    if args.model:
        CONFIG['PRINTER']['MODEL'] = args.model

    if args.default_label_size:
        CONFIG['LABEL']['DEFAULT_SIZE'] = args.default_label_size

    if args.default_orientation:
        CONFIG['LABEL']['DEFAULT_ORIENTATION'] = args.default_orientation

    if args.font_folder:
        CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER'] = args.font_folder

# setup logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=CONFIG['SERVER']['LOGLEVEL'], format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# get list of label sizes
LABEL_SIZES = [(name, label_type_specs[name]['name']) for name in label_sizes]

# select and validate printing backend
try:
    selected_backend = guess_backend(CONFIG['PRINTER']['PRINTER'])
except ValueError:
    parser.error("Couldn't guess the backend to use from the printer string descriptor")

BACKEND_CLASS = backend_factory(selected_backend)['backend_class']

# validate default label size
if CONFIG['LABEL']['DEFAULT_SIZE'] not in label_sizes:
    parser.error("Invalid --default-label-size. Please choose on of the following:\n:" + " ".join(label_sizes))

# setup fonts
FONTS = get_fonts()
if CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']:
    FONTS.update(get_fonts(CONFIG['SERVER']['ADDITIONAL_FONT_FOLDER']))

if not FONTS:
    logger.critical(
        'Not a single font was found on your system. Please install some or use the "--font-folder" argument.'
    )
    sys.exit(2)

# setting default font
for font in CONFIG['LABEL']['DEFAULT_FONTS']:
    try:
        FONTS[font['family']][font['style']]
        CONFIG['LABEL']['DEFAULT_FONT'] = font
        logger.debug("Selected the following default font: {}".format(font))
        break
    except BaseException:
        pass

if CONFIG['LABEL']['DEFAULT_FONT'] is None:
    logger.error('Could not find any of the default fonts.')
    logger.warning('Choosing a random font as default.')
    family = random.choice(list(FONTS.keys()))
    style = random.choice(list(FONTS[family].keys()))
    CONFIG['LABEL']['DEFAULT_FONT'] = {'family': family, 'style': style}
    logger.warning('The default font is now set to: {family} ({style})'.format(**CONFIG['LABEL']['DEFAULT_FONT']))


# functions #
# ######### #

# convert image to byte string
def image_to_png_bytes(im):
    image_buffer = BytesIO()
    im.save(image_buffer, format="PNG")
    image_buffer.seek(0)
    return image_buffer.read()


# get label context
def get_label_context(request):
    """ might raise LookupError() """

    d = request.params.decode() # UTF-8 decoded form data

    font_family = d.get('font_family').rpartition('(')[0].strip()
    font_style = re.search(r'\((\w*)\)', d.get('font_family'))[1]
    logger.debug('Using font {} with style {}'.format(font_family, font_style))
    context = {
      'text':          d.get('text', None),
      'font_size': int(d.get('font_size', 100)),
      'font_family':   font_family,
      'font_style':    font_style,
      'label_size':    d.get('label_size', "62"),
      'kind':          label_type_specs[d.get('label_size', "62")]['kind'],
      'margin':    int(d.get('margin', 10)),
      'threshold': int(d.get('threshold', 70)),
      'align':         d.get('align', 'center'),
      'orientation':   d.get('orientation', 'standard'),
      'margin_top':    float(d.get('margin_top',    24))/100.,
      'margin_bottom': float(d.get('margin_bottom', 45))/100.,
      'margin_left':   float(d.get('margin_left',   35))/100.,
      'margin_right':  float(d.get('margin_right',  35))/100.,
    }
    context['margin_top'] = int(context['font_size']*context['margin_top'])
    context['margin_bottom'] = int(context['font_size']*context['margin_bottom'])
    context['margin_left'] = int(context['font_size']*context['margin_left'])
    context['margin_right'] = int(context['font_size']*context['margin_right'])

    def get_font_path(font_family_name, font_style_name):
        try:
            if font_family_name is None or font_style_name is None:
                font_family_name = CONFIG['LABEL']['DEFAULT_FONTS']['family'].strip()
                font_style_name = CONFIG['LABEL']['DEFAULT_FONTS']['style'].strip()
            font_path = FONTS[font_family_name][font_style_name]
        except KeyError:
            logger.critical('Could not find the font & style ({} - {})'.format(font_family_name, font_style_name))
            raise LookupError('Could not find the font & style')
        return font_path

    context['font_path'] = get_font_path(context['font_family'], context['font_style'])

    def get_label_dimensions():
        try:
            ls = label_type_specs[context['label_size']]
        except KeyError:
            raise LookupError("Unknown label_size")
        return ls['dots_printable']

    width, height = get_label_dimensions()
    if height > width: width, height = height, width
    if context['orientation'] == 'rotated':
        height, width = width, height
    context['width'], context['height'] = width, height

    return context


# create label image
def create_label_im(text, **kwargs):
    label_type = kwargs['kind']
    im_font = ImageFont.truetype(kwargs['font_path'], kwargs['font_size'])
    im = Image.new('L', (20, 20), 'white')
    draw = ImageDraw.Draw(im)
    # workaround for a bug in multiline_textsize()
    # when there are empty lines in the text:
    lines = []
    for line in text.split('\n'):
        if line == '': line = ' '
        lines.append(line)
    text = '\n'.join(lines)
    textsize = draw.multiline_textsize(text, font=im_font)
    width, height = kwargs['width'], kwargs['height']
    if kwargs['orientation'] == 'standard':
        if label_type in (ENDLESS_LABEL,):
            height = textsize[1] + kwargs['margin_top'] + kwargs['margin_bottom']
    elif kwargs['orientation'] == 'rotated':
        if label_type in (ENDLESS_LABEL,):
            width = textsize[0] + kwargs['margin_left'] + kwargs['margin_right']
    im = Image.new('L', (width, height), 'white')
    draw = ImageDraw.Draw(im)
    if kwargs['orientation'] == 'standard':
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            vertical_offset  = (height - textsize[1])//2
            vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        else:
            vertical_offset = kwargs['margin_top']
        horizontal_offset = max((width - textsize[0])//2, 0)
    elif kwargs['orientation'] == 'rotated':
        vertical_offset  = (height - textsize[1])//2
        vertical_offset += (kwargs['margin_top'] - kwargs['margin_bottom'])//2
        if label_type in (DIE_CUT_LABEL, ROUND_DIE_CUT_LABEL):
            horizontal_offset = max((width - textsize[0])//2, 0)
        else:
            horizontal_offset = kwargs['margin_left']
    offset = horizontal_offset, vertical_offset
    draw.multiline_text(offset, text, (0), font=im_font, align=kwargs['align'])
    return im


# bottle routing #
# ############## #

# default route
@route('/')
def index():
    redirect('/labeldesigner')


# serve static files TODO: refactor, current implementation is a security thread
@route('/static/<filename:path>')
def serve_static(filename):
    return static_file(filename, root='./static')


# serve label designer
@route('/labeldesigner')
@view('labeldesigner.jinja2')
def labeldesigner():
    font_family_names = sorted(list(FONTS.keys()))
    return {'font_family_names': font_family_names,
            'fonts': FONTS,
            'label_sizes': LABEL_SIZES,
            'website': CONFIG['WEBSITE'],
            'label': CONFIG['LABEL']}


# api call for preview image
@get('/api/preview/text')
@post('/api/preview/text')
def get_preview_image():
    context = get_label_context(request)
    im = create_label_im(**context)
    return_format = request.query.get('return_format', 'png')
    if return_format == 'base64':
        import base64
        response.set_header('Content-type', 'text/plain')
        return base64.b64encode(image_to_png_bytes(im))
    else:
        response.set_header('Content-type', 'image/png')
        return image_to_png_bytes(im)


# api call for printing text
@post('/api/print/text')
@get('/api/print/text')
def print_text():
    """
    API to print a label

    returns: JSON

    Ideas for additional URL parameters:
    - alignment
    """

    return_dict = {'success': False}

    try:
        context = get_label_context(request)
    except LookupError as e:
        return_dict['error'] = e.msg
        return return_dict

    if context['text'] is None:
        return_dict['error'] = 'Please provide the text for the label'
        return return_dict

    im = create_label_im(**context)
    if DEBUG: im.save('sample-out.png')

    if context['kind'] == ENDLESS_LABEL:
        rotate = 0 if context['orientation'] == 'standard' else 90
    elif context['kind'] in (ROUND_DIE_CUT_LABEL, DIE_CUT_LABEL):
        rotate = 'auto'

    qlr = BrotherQLRaster(CONFIG['PRINTER']['MODEL'])
    create_label(qlr, im, context['label_size'], threshold=context['threshold'], cut=True, rotate=rotate)

    if not DEBUG:
        try:
            be = BACKEND_CLASS(CONFIG['PRINTER']['PRINTER'])
            be.write(qlr.data)
            be.dispose()
            del be
        except Exception as e:
            return_dict['message'] = str(e)
            logger.warning('Exception happened: %s', e)
            return return_dict

    return_dict['success'] = True
    if DEBUG:
        return_dict['data'] = str(qlr.data)
    return return_dict


def main():
    run(host=CONFIG['SERVER']['HOST'], port=PORT, debug=DEBUG)


if __name__ == "__main__":
    run(host=CONFIG['SERVER']['HOST'], port=CONFIG['SERVER']['PORT'], debug=DEBUG)
